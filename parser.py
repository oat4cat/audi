import json
import random
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import httpx
from bs4 import BeautifulSoup
import os

BASE_URL = "https://ruz.narfu.ru/"
REQUEST_ATTEMPTS = 2
MAX_CONSECUTIVE_GROUP_FAILURES = 3


class ScheduleFetchError(Exception):
    """Не удалось получить корректную страницу расписания после повторов."""


class ScheduleSiteUnavailableError(ScheduleFetchError):
    """Сайт расписания недоступен для продолжения текущего прогона."""


@dataclass
class ConsecutiveFailureTracker:
    """Отслеживает подряд идущие ошибки групп для остановки полного обхода."""

    threshold: int
    count: int = 0

    def record_success(self) -> None:
        self.count = 0

    def record_failure(self) -> bool:
        self.count += 1
        return self.count >= self.threshold


def is_schedule_page(html_text: str) -> bool:
    """Проверяет, что полученный HTML похож на страницу расписания группы."""
    soup = BeautifulSoup(html_text, "html.parser")
    return bool(
        soup.select_one("a.navbar-brand")
        and soup.select("div.list:has(.dayofweek)")
    )


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
    client: httpx.Client,
    group_id: str,
    max_attempts: int = REQUEST_ATTEMPTS,
    retry_delay: float = 2.0,
) -> str:
    """Запрашивает страницу группы, повторяя разовый сбой ровно один раз."""
    url = f"{BASE_URL}?timetable&group={group_id}"
    last_error = "неизвестная ошибка"

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.get(url, timeout=15.0)
            if 500 <= response.status_code < 600:
                raise ScheduleSiteUnavailableError(
                    f"сервер вернул HTTP {response.status_code}"
                )

            if response.status_code == 200:
                if is_schedule_page(response.text):
                    return response.text
                last_error = "сервер не отдал страницу расписания"
            else:
                last_error = f"статус {response.status_code}"
        except ScheduleSiteUnavailableError:
            raise
        except httpx.HTTPError as error:
            last_error = f"ошибка сети ({error})"

        print(
            f"   [!] Группа {group_id}: {last_error} "
            f"(попытка {attempt}/{max_attempts})"
        )
        if attempt < max_attempts:
            time.sleep(retry_delay)

    raise ScheduleFetchError(
        f"группа {group_id}: {last_error} после {max_attempts} попыток"
    )


def parse_group_html(html_text: str, group_id: str) -> list[dict]:
    """Извлекает все найденные занятия из HTML группы."""
    soup = BeautifulSoup(html_text, "html.parser")
    parsed_slots = []

    day_blocks = soup.select("div.list:has(.dayofweek)")
    group_full = soup.select_one("a.navbar-brand")
    if group_full:
        group_full_name = group_full.get_text(" ", strip=True).split('/')[-1].strip()
    else:
        group_full_name = group_id  

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
            group_name = group_elem.get_text(strip=True) if group_elem else group_full_name

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
                # Ищем скобки, в которых лежит Фамилия И.О. (наличие точки после инициала)
                lect_match = re.search(r"\(([^)]*?\b[А-ЯA-Z][а-яa-z\-]+\s+[А-ЯA-Z]\.[А-ЯA-Z]\.)\)", disc_text)
                if lect_match:
                    lecturer_masked = mask_lecturer_name(lect_match.group(1))
                else:
                    # Резервный поиск последних скобок в строке, если форма инициалов чуть отличается
                    all_matches = re.findall(r"\(([^)]+)\)", disc_text)
                    if all_matches:
                        lecturer_masked = mask_lecturer_name(all_matches[-1])

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


def extract_group_names(html_text: str) -> set[str]:
    """Возвращает варианты названия группы, встречающиеся на странице."""
    soup = BeautifulSoup(html_text, "html.parser")
    group_names = set()

    brand = soup.select_one("a.navbar-brand")
    if brand:
        group_names.add(brand.get_text(" ", strip=True).split("/")[-1].strip())

    for group_elem in soup.select("div.timetable_sheet:not(.transparent) .group"):
        group_names.add(group_elem.get_text(strip=True))

    return {name for name in group_names if name}


def normalize_group_name(group_name: str) -> str:
    return re.sub(r"\s+", " ", group_name).strip().casefold()


def remove_group_slots(
    auditories_db: dict, group_id: str, group_names: set[str]
) -> None:
    """Удаляет старые слоты одной группы после успешного обновления."""
    normalized_names = {normalize_group_name(name) for name in group_names}

    for auditory in list(auditories_db.values()):
        schedule = auditory.get("schedule", {})
        for date in list(schedule):
            slots = schedule[date]
            for num_para in list(slots):
                slot = slots[num_para]
                belongs_to_group = slot.get("source_group_id") == group_id
                belongs_to_group |= (
                    normalize_group_name(slot.get("group", "")) in normalized_names
                )
                if belongs_to_group:
                    del slots[num_para]

            if not slots:
                del schedule[date]


def merge_group_slots(
    auditories_db: dict, slots: list[dict], group_id: str, now_iso: str
) -> None:
    """Добавляет актуальные занятия одной группы в общую базу."""
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
            "source_group_id": group_id,
            "updated_at": now_iso,
        }
        existing_slot = auditories_db[aud]["schedule"][date].get(num)

        if existing_slot != new_slot_data:
            auditories_db[aud]["schedule"][date][num] = new_slot_data
            auditories_db[aud]["updated_at"] = now_iso


def reconcile_schedules(
    old_data: dict,
    successful_groups: dict[str, tuple[list[dict], set[str]]],
    failed_group_ids: set[str],
    now_iso: str,
) -> dict:
    """Обновляет успешные группы и сохраняет данные только при явной ошибке."""
    if failed_group_ids:
        auditories_db = deepcopy(old_data)
        for group_id, (_, group_names) in successful_groups.items():
            remove_group_slots(auditories_db, group_id, group_names)
    else:
        auditories_db = {}

    for group_id, (slots, _) in successful_groups.items():
        merge_group_slots(auditories_db, slots, group_id, now_iso)

    for aud in list(auditories_db):
        if not auditories_db[aud].get("schedule"):
            del auditories_db[aud]

    return auditories_db


def collect_group_schedules(
    client: httpx.Client,
    groups: list[str],
    failure_threshold: int = MAX_CONSECUTIVE_GROUP_FAILURES,
) -> tuple[dict[str, tuple[list[dict], set[str]]], set[str]]:
    """Собирает группы, останавливая обход при признаках полного отказа сайта."""
    successful_groups = {}
    failed_group_ids = set()
    failures = ConsecutiveFailureTracker(failure_threshold)

    for idx, group_id in enumerate(groups, 1):
        print(f"[{idx}/{len(groups)}] Запрос группы {group_id}...")

        try:
            html_text = fetch_group_schedule_with_retry(client, group_id)
        except ScheduleSiteUnavailableError as error:
            raise ScheduleSiteUnavailableError(
                f"группа {group_id}: {error}"
            ) from error
        except ScheduleFetchError as error:
            print(
                f"   [WARN] {error}. Сохраняем старые данные для этой группы."
            )
            failed_group_ids.add(group_id)
            if failures.record_failure():
                raise ScheduleSiteUnavailableError(
                    f"{failures.count} ошибок получения страниц подряд"
                ) from error
            continue

        failures.record_success()
        successful_groups[group_id] = (
            parse_group_html(html_text, group_id),
            extract_group_names(html_text),
        )

        time.sleep(random.uniform(0.8, 1.8))

    return successful_groups, failed_group_ids


def main():
    now_iso = datetime.now().isoformat()

    # 1. Загружаем старую базу, если она существует
    os.makedirs("data", exist_ok=True)
    db_file = "data/auditories.json"
    try:
        with open(db_file, "r", encoding="utf-8") as f:
            old_data = json.load(f)
            auditories_db = old_data.get("auditories", {})
    except (FileNotFoundError, json.JSONDecodeError):
        auditories_db = {}

    # Загружаем список групп
    with open("data/groups.json", "r", encoding="utf-8") as f:
        groups = json.load(f)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
    }

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        try:
            successful_groups, failed_group_ids = collect_group_schedules(
                client, groups
            )
        except ScheduleSiteUnavailableError as error:
            print(
                f"\n[STOP] Сайт расписания недоступен: {error}. "
                "Старая база сохранена без изменений; следующая попытка — "
                "по регулярному расписанию."
            )
            return

    auditories_db = reconcile_schedules(
        auditories_db,
        successful_groups,
        failed_group_ids,
        now_iso,
    )

    # Формируем итоговый JSON
    output_data = {"global_updated_at": now_iso, "auditories": auditories_db}

    with open(db_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("\nПарсинг завершен! База данных успешно обновлена.")


if __name__ == "__main__":
    main()