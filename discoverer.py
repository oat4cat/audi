import json
import random
import re
import time
import httpx
from bs4 import BeautifulSoup
import os

BASE_URL = "https://ruz.narfu.ru/"

# Настройки фильтрации институтов
EXCLUDED_INSTITUTIONS = {"23", "26", "6", "35"}  # пропускаем
FULL_SCAN_INSTITUTIONS = {"3"}  # парсим целиком
NPK_INSTITUTION = "33"  # Аспирантура/НПК — только 2.3.1


def get_institutions_to_scan(client: httpx.Client) -> list[tuple[str, str]]:
    """
    Собирает список Высших школ с главной страницы.
    Возвращает список кортежей: (inst_id, inst_path)
    """
    try:
        response = client.get(BASE_URL, timeout=15.0)
        response.raise_for_status()
    except Exception as e:
        print(f"Ошибка при загрузке главной страницы: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    institutions = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        match = re.search(r"institution=(\d+)", href)
        if match:
            inst_id = match.group(1)

            # Пропускаем филиалы
            if inst_id in EXCLUDED_INSTITUTIONS:
                continue

            clean_href = href.lstrip("/")
            institutions.append((inst_id, clean_href))

    # Удаляем дубликаты
    unique_insts = list(dict.fromkeys(institutions))
    print(
        f"Найдено Высших школ для сканирования: {len(unique_insts)} (Северодвинск исключен)"
    )
    return unique_insts


def parse_groups_from_institution(
    client: httpx.Client, inst_id: str, inst_path: str
) -> set[str]:
    """Парсит группы конкретной Высшей школы согласно правилам таргетинга."""
    url = f"{BASE_URL}{inst_path}"
    groups = set()

    try:
        response = client.get(url, timeout=15.0)
        response.raise_for_status()
    except Exception as e:
        print(f"Ошибка при запросе Высшей школы {inst_id} ({inst_path}): {e}")
        return groups

    soup = BeautifulSoup(response.text, "html.parser")

    # СЦЕНАРИЙ 1: Управление подготовки НПК (id = 33)
    if inst_id == NPK_INSTITUTION:
        # Ищем карточки/ссылки, где <span class="number"> содержит "2.3.1"
        for a_tag in soup.find_all("a", href=True):
            if "group=" not in a_tag["href"]:
                continue

            num_span = a_tag.find("span", class_="number")
            if num_span and "2.3.1" in num_span.get_text(strip=True):
                match = re.search(r"group=(\d+)", a_tag["href"])
                if match:
                    groups.add(match.group(1))

        print(f"   [НПК 2.3.1] Найдено целевых групп: {len(groups)}")
        return groups

    # СЦЕНАРИЙ 2: (id = 3) — забираем все группы со всех курсов
    if inst_id in FULL_SCAN_INSTITUTIONS:
        for a_tag in soup.find_all("a", href=True):
            match = re.search(r"group=(\d+)", a_tag["href"])
            if match:
                groups.add(match.group(1))

        print(f"   [ВШИТиАС Полный] Найдено групп: {len(groups)}")
        return groups

    # СЦЕНАРИЙ 3: Все остальные Высшие школы — берем ТОЛЬКО 1 и 2 курс
    # Ищем блоки вкладок id="course_1" и id="course_2"
    for course_id in ["course_1", "course_2"]:
        course_pane = soup.find("div", id=course_id)
        if not course_pane:
            continue

        for a_tag in course_pane.find_all("a", href=True):
            match = re.search(r"group=(\d+)", a_tag["href"])
            if match:
                groups.add(match.group(1))

    print(f"   [1 и 2 курс] Найдено групп: {len(groups)}")
    return groups


def discover_targeted_groups():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
    }

    all_group_ids = set()

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        institutions = get_institutions_to_scan(client)

        for idx, (inst_id, inst_path) in enumerate(institutions, 1):
            print(
                f"[{idx}/{len(institutions)}] Обработка Высшей школы ID={inst_id}..."
            )

            inst_groups = parse_groups_from_institution(
                client, inst_id, inst_path
            )
            all_group_ids.update(inst_groups)

            time.sleep(random.uniform(0.8, 1.5))

    sorted_groups = sorted(list(all_group_ids), key=int)

    print("\n--- Итоги сфокусированного сбора ---")
    print(f"Всего целевых групп в списке: {len(sorted_groups)}")

    os.makedirs("data", exist_ok=True)
    with open("data/groups.json", "w", encoding="utf-8") as f:
        json.dump(sorted_groups, f, ensure_ascii=False, indent=2)

    print("Обновленный список сохранен в data/groups.json")


if __name__ == "__main__":
    discover_targeted_groups()