import json
import random
import re
import time
from datetime import datetime
import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://ruz.narfu.ru/"


def mask_lecturer_name(full_name: str) -> str:
    full_name = full_name.strip("() ")
    parts = full_name.split()
    if not parts:
        return ""

    surname = parts[0]
    initials = " ".join(parts[1:]) if len(parts) > 1 else ""

    if len(surname) <= 4:
        masked_surname = surname[0] + "****"
    else:
        masked_surname = surname[:3] + "****" + surname[-1]

    return f"{masked_surname} {initials}".strip()

def resolve_building(aud_text: str) -> str | None:
    """
    Определяет номер корпуса по окончанию строки аудитории:
    - заканчивается на 'корп. 2' -> 10 корпус
    - заканчивается на 'ы, д. 2'  -> 11 корпус
    - для всех остальных адресов возвращает None (пропускаем)
    """
    aud_clean = aud_text.strip()
    
    if aud_clean.endswith("корп. 2"):
        room_number = aud_clean.split(',')[0].strip()
        return f"{room_number} (10 корпус)"
    
    if aud_clean.endswith("ы, д. 2"):
        room_number = aud_clean.split(',')[0].strip()
        return f"{room_number} (11 корпус)"
    
    # Все остальные корпуса отсекаем
    return None


def fetch_group_schedule_with_retry(
    client: httpx.Client, group_id: str, max_retries: int = 3
) -> str | None:
    """Запрашивает расписание группы с механизмом повторов при таймаутах."""
    url = f"{BASE_URL}?timetable&group={group_id}"

    for attempt in range(1, max_retries + 1):
        try:
            response = client.get(url, timeout=15.0)
            if response.status_code == 200:
                return response.text
            print(
                f"   [!] Группа {group_id}: Статус {response.status_code} (попытка {attempt}/{max_retries})"
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.HTTPError) as e:
            print(
                f"   [!] Группа {group_id}: Ошибка сети ({e}) (попытка {attempt}/{max_retries})"
            )

        if attempt < max_retries:
            time.sleep(2.0 * attempt)  # Нарастающая пауза перед повтором

    return None


def parse_group_html(html_text: str, group_id: str) -> list[dict]:
    """Извлекает все найденные занятия из HTML группы."""
    soup = BeautifulSoup(html_text, "html.parser")
    parsed_slots = []

    day_blocks = soup.select("div.list.col-md-2")

    for day in day_blocks:
        day_text_elem = day.select_one(".dayofweek")
        if not day_text_elem:
            continue

        day_raw = day_text_elem.get_text(strip=True)
        date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", day_raw)
        if not date_match:
            continue
        date_str = date_match.group(0)

        # Парсим десктопные карточки
        sheets = day.select("div.timetable_sheet:not(.transparent)")
        for sheet in sheets:
            num_elem = sheet.select_one(".num_para")
            time_elem = sheet.select_one(".time_para")
            aud_elem = sheet.select_one(".auditorium")
            disc_elem = sheet.select_one(".discipline")
            kind_elem = sheet.select_one(".kindOfWork")
            group_elem = sheet.select_one(".group")

            if not (num_elem and aud_elem):
                continue

            num_para = num_elem.get_text(strip=True)
            time_para = time_elem.get_text(strip=True) if time_elem else "Не распознано"
            aud_raw = aud_elem.get_text(" ", strip=True)
            group_name = group_elem.get_text(strip=True) if group_elem else "Не распознано"

            aud_clean = re.sub(
                r"^ауд\.\s*", "", aud_raw, flags=re.IGNORECASE
            ).strip()

            # Игнорируем дистанционку и онлайн-платформы
            if any(
                ignore in aud_clean.lower()
                for ignore in [
                    "дистанционное",
                    "сдо",
                    "sakai",
                    "онлайн",
                    "teams",
                    "zoom",
                ]
            ):
                continue

            aud_clean = resolve_building(aud_clean)
            if aud_clean is None:
                continue

            lecturer_masked = ""
            if disc_elem:
                disc_text = disc_elem.get_text(" ", strip=True)
                lect_match = re.search(r"\(([^)]+)\)", disc_text)
                if lect_match:
                    lecturer_masked = mask_lecturer_name(lect_match.group(1))

            discipline_name = (
                re.sub(r"\([^)]*\)", "", disc_elem.get_text(strip=True)).strip()
                if disc_elem
                else ""
            )
            kind_work = kind_elem.get_text(strip=True) if kind_elem else ""

            parsed_slots.append({
                "auditory": aud_clean,
                "date": date_str,
                "num_para": num_para,
                "time": time_para,
                "discipline": discipline_name,
                "kind": kind_work,
                "lecturer": lecturer_masked,
                "group": group_name,
            })

    return parsed_slots


def main():
    now_iso = datetime.now().isoformat()

    # 1. Загружаем старую базу, если она существует
    db_file = "auditories.json"
    try:
        with open(db_file, "r", encoding="utf-8") as f:
            old_data = json.load(f)
            auditories_db = old_data.get("auditories", {})
    except (FileNotFoundError, json.JSONDecodeError):
        auditories_db = {}

    # Загружаем список групп
    with open("groups.json", "r", encoding="utf-8") as f:
        groups = json.load(f)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
    }

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for idx, group_id in enumerate(groups, 1):
            print(f"[{idx}/{len(groups)}] Запрос группы {group_id}...")

            html_text = fetch_group_schedule_with_retry(client, group_id)

            if html_text is None:
                print(
                    f"   [WARN] Не удалось получить данные для группы {group_id}. Сохраняем старые данные."
                )
                continue

            # Извлекаем все свежие пары группы
            slots = parse_group_html(html_text, group_id)

            # Вливаем свежие данные в общую структуру
            for slot in slots:
                aud = slot["auditory"]
                date = slot["date"]
                num = slot["num_para"]

                if aud not in auditories_db:
                    auditories_db[aud] = {
                        "updated_at": now_iso,
                        "schedule": {},
                    }

                if date not in auditories_db[aud]["schedule"]:
                    auditories_db[aud]["schedule"][date] = {}

                new_slot_data = {
                    "time": slot["time"],
                    "status": "busy",
                    "discipline": slot["discipline"],
                    "kind": slot["kind"],
                    "lecturer": slot["lecturer"],
                    "group": slot["group"],
                    "updated_at": now_iso,  # Время последнего успешного обновления этой конкретной пары
                }

                # Сравниваем с тем, что уже лежало
                existing_slot = (
                    auditories_db[aud]["schedule"].get(date, {}).get(num)
                )

                # Если данные изменились или их не было — обновляем слот и время у всей аудитории
                if existing_slot != new_slot_data:
                    auditories_db[aud]["schedule"][date][num] = new_slot_data
                    auditories_db[aud]["updated_at"] = now_iso

            # Пауза между группами
            time.sleep(random.uniform(0.8, 1.8))

    # Формируем итоговый JSON
    output_data = {"global_updated_at": now_iso, "auditories": auditories_db}

    with open(db_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("\nПарсинг завершен! База данных успешно обновлена.")


if __name__ == "__main__":
    main()