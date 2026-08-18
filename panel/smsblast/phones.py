"""Нормализация номеров и подсчёт SMS-сегментов."""

import re

# Базовый набор GSM 03.38: если весь текст помещается в него — 160 символов
# на сегмент, иначе (любая кириллица) кодировка UCS-2 и всего 70.
GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXTENDED = set("^{}\\[~]|€")  # занимают по 2 позиции

_DIGITS_RE = re.compile(r"\D")


def normalize(raw, default_country_code="7"):
    """Приводит номер к формату E.164 (+79001234567).

    Возвращает (номер, None) при успехе или (None, причина) при отбраковке.
    """
    if raw is None:
        return None, "пусто"

    value = str(raw).strip()
    if not value:
        return None, "пусто"

    # Номера из Excel часто приезжают как 79001234567.0
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]

    has_plus = value.lstrip().startswith("+")
    digits = _DIGITS_RE.sub("", value)
    if not digits:
        return None, "нет цифр"

    if not has_plus:
        # Российский формат: 8 (900) 123-45-67 → +7 900 123-45-67
        if len(digits) == 11 and digits[0] == "8" and default_country_code == "7":
            digits = "7" + digits[1:]
        elif len(digits) == 10:
            digits = default_country_code + digits

    if len(digits) < 8 or len(digits) > 15:
        return None, "неверная длина ({} цифр)".format(len(digits))

    return "+" + digits, None


def encoding_of(text):
    """'GSM-7' или 'UCS-2' — от этого зависит длина сегмента."""
    for ch in text:
        if ch not in GSM_BASIC and ch not in GSM_EXTENDED:
            return "UCS-2"
    return "GSM-7"


def segments(text):
    """(кодировка, число символов, число SMS-сегментов).

    Кириллица переводит сообщение в UCS-2: 70 символов в одиночном SMS
    и по 67 в каждой части длинного — это напрямую умножает стоимость.
    """
    if not text:
        return "GSM-7", 0, 0

    enc = encoding_of(text)
    if enc == "GSM-7":
        length = sum(2 if ch in GSM_EXTENDED else 1 for ch in text)
        single, multi = 160, 153
    else:
        length = len(text)
        single, multi = 70, 67

    count = 1 if length <= single else -(-length // multi)  # ceil
    return enc, length, count
