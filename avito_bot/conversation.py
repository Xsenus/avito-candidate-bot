from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from .avito_client import AvitoClient
from .candidate import normalize_phone, resolve_internship_date, split_full_name


@dataclass
class ConversationState:
    step: str = "idle"
    chosen_day: str | None = None
    full_name: str | None = None
    last_name: str | None = None
    first_name: str | None = None
    phone: str | None = None
    city: str | None = None
    item_id: str | None = None
    service_center: str | None = None
    address: str | None = None
    date_time: str | None = None
    internship_date: str | None = None
    tariff: str = "Драйв"
    citizenship: str = field(
        default_factory=lambda: os.getenv(
            "YANDEX_FORM_CITIZENSHIP", "Российская Федерация"
        )
    )
    notes: dict[str, str] = field(default_factory=dict)
    resume_step: str | None = None
    application_status: str = "collecting"
    processing_notice_sent: bool = False
    last_error: str | None = None
    submission_attempts: int = 0
    next_retry_at: str | None = None


ADDRESS_FALLBACK = "Точный адрес склада уточнит координатор после записи."

ADDRESS_BY_CITY = {
    "астрахань": "Астрахань, 1-й проезд Рождественского, с8",
    "барнаул": "Барнаул, Чернышевского 293 Б",
    "белгород": "г. Белгород, ул Мичурина 104А",
    "брянск": "Брянская область, г.Брянск, ул Сталелитейная, д 1",
    "владимир": "Владимир, улица Куйбышева, 28Е ТЦ Подкова",
    "волгоград": "г. Волгоград, ул. Землячки, 94",
    "вологда": "г. Вологда, Ананьинский переулок, 14",
    "воронеж": "Воронежская область, Рамонский муниципальный район, Айдаровское сельское поселение, территория Промышленная, ул. 2-я Промышленная зона, 27",
    "екатеринбург": "г. Екатеринбург, Серовский тракт, 11-й километр, с1",
    "иваново": "г. Иваново, ул. Станкостроителей, 5",
    "ижевск": "г. Ижевск, ул. Пойма, 105",
    "казань": "Лаишевский район, С. Столбище, улица Почтовая, дом 1",
    "калуга": "Калуга ул. Дальняя, 17",
    "кемерово": "Кемерово, ул. Терешковой д.41/11",
    "киров": "г. Киров, ул. П.Корчагина, д 225/2",
    "краснодар": "г. Краснодар, х. Октябрьский, ул. Подсолнечная, 44",
    "красноярск": "г.Красноярск, Промысловая ул. 41 а",
    "курск": "г. Курск пр-т Ленинского Комсомола д.49",
    "лазаревское": "переулок Павлова, 6уч1",
    "липецк": "г. Липецк, Базарная улица, 3к",
    "москва": "г. Москва, Дмитровское шоссе 157с1",
    "мск": "г. Москва, Дмитровское шоссе 157с1",
    "новосибирск": "Архонский переулок, 2Ак6",
    "омск": "г. Омск, ул. 22 Декабря, дом 108А",
    "оренбург": "г. Оренбург, Беляевская улица, 4/4",
    "пенза": "Пенза, ул. Зеленодольская, 56",
    "пермь": "Пермь, Героев Хасана 98к5",
    "санкт-петербург": "г. Санкт-Петербург, Запорожская улица, д.12, строение 1",
    "спб": "г. Санкт-Петербург, Запорожская улица, д.12, строение 1",
    "питер": "г. Санкт-Петербург, Запорожская улица, д.12, строение 1",
    "пятигорск": "поселок Горячеводский, ул. Ясная, здание 21, строение 2",
    "ростов": "Аксайский район, Новочеркасское шоссе 111к2",
    "рязань": "г. Рязань, ул. Чкалова, д. 36B",
    "самара": "Индустриальная 2а/5, село Преображенка, Волжский район, Самарская область",
    "саратов": "г. Саратов, ул автокомбинатовская 12, ст 3",
    "смоленск": "г. Смоленск, Краснинское шоссе, д. 27",
    "сочи": "г. Сочи, переулок Виноградный, 15",
    "ставрополь": "г. Ставрополь, ул. Старомарьевское шоссе, д. 13/2",
    "тверь": "Улица Бочкина 17",
    "томск": "г.Томск, Мокрушина 9 стр 21",
    "тула": "г. Тула, улица Щегловская Засека, 31",
    "тюмень": "город Тюмень, улица 30 лет Победы, дом 6 корпус 3",
    "ульяновск": "г. Ульяновск, Магистральная ул, 1",
    "уфа": "г. Уфа, улица Менделеева, дом 134Е",
    "чебоксары": "Чебоксары, Гаражный проезд 3/1",
    "челябинск": "г. Челябинск, ул. Монтажников, д.16",
    "ярославль": "г. Ярославль, ул. Осташинская, д. 8",
    "новороссийск": "г. Новороссийск, Мысхакское шоссе, 57А",
}

INITIAL_MESSAGE = (
    "Вы вовремя откликнулись 👌\n"
    "Сейчас отправлю детали по вакансии.\n\n"
    "На этой неделе у нас изменились условия в лучшую сторону: авто компании теперь предоставляем бесплатно, бензин и обслуживание - за наш счёт.\n\n"
    "Расскажу подробнее в течении 1 минуты, ожидайте сообщения здесь"
)

FOLLOW_UP_MESSAGE = (
    "Доброго времени суток!\n"
    "Спасибо за интерес к вакансии!\n\n"
    "Мы Яндекс Маркет — работа со складов до ПВЗ, постаматов и клиентов.🚚\n"
    "Даём авто в аренду бесплатно (Ford Transit МКПП), возможно домашнее хранение. Расходы на бензин, парковки или обслуживание все за наш счет. Ваш доход — это полностью ваш доход.\n\n"
    "🔻Доход считается за рейс, 1 рейс от 4 400,00.\n\n"
    "🔻Ваша задача — утром загрузиться на складе и развести товар(мелкие посылки) по пунктам выдачи. После обеда возможна вторая загрузка.\n"
    "Первая загрузка строго утром.\n\n"
    "🔻График работы индивидуальный, подбираете самостоятельно. Оформление возможно по СМЗ или ГПХ. Выплаты 2 раза в месяц, возможно на любую карту. Доход до 160 000р в мес.\n\n"
    "Если вам интересно — напишите «Да»."
)

INTERNSHIP_MESSAGE = (
    "У нас предусмотрена стажировка, на которой бригадир покажет вам процесс работы, а после можно будет забрать машину и начать работу.\n"
    "Стажировка начинается строго утром и занимает от 4х до 6 часов. После нее сможете перейти к оформлению.\n\n"
    "Готовы пройти стажировку? Напишите «Да»"
)






ADDRESS_MESSAGE = (
    "Стажировка каждый день в 8 утра, на какой день вас записать? Укажите день недели например: Вторник"
)

STORE_SELECTION_MESSAGE = (
    "Стажировка каждый день в 8 утра, на какой день вас записать? Укажите день недели например: Вторник"
   )

CONFIRMATION_MESSAGE = (
    "Для пропуска пришлите Фамилию Имя, без пропуска вы не сможете попасть на склад. Напишите это сейчас"
)


def handle_user_message(state: ConversationState, text: str, client: AvitoClient | None = None, chat_id: str | None = None, city_hint: str | None = None) -> str:
    cleaned = (text or "").strip().lower()

    if asks_for_address(cleaned):
        state.city = normalize_city(city_hint) or state.city
        address = resolve_address(state.city)
        state.address = address
        state.step = "awaiting_arrival"
        return ADDRESS_MESSAGE

    if state.step == "idle":
        state.step = "awaiting_interest"
        return INITIAL_MESSAGE

    if state.step == "awaiting_interest":
        if is_positive(cleaned):
            state.step = "awaiting_staj"
            return INTERNSHIP_MESSAGE
        return INTERNSHIP_MESSAGE

    if state.step == "awaiting_staj":
        if looks_like_datetime(cleaned):
            state.date_time = text.strip()
            state.internship_date = resolve_internship_date(text).strftime("%d.%m.%Y")
            state.step = "awaiting_full_name"
            return CONFIRMATION_MESSAGE
        if is_positive(cleaned):
            state.step = "awaiting_arrival"
            state.city = normalize_city(city_hint) or state.city
            address = resolve_address(state.city)
            state.address = address
            return ADDRESS_MESSAGE.format(address=address)
        return "Готовы пройти стажировку?"

    if state.step == "awaiting_arrival":
        if looks_like_datetime(cleaned):
            state.date_time = text.strip()
            state.internship_date = resolve_internship_date(text).strftime("%d.%m.%Y")
            state.step = "awaiting_full_name"
            return CONFIRMATION_MESSAGE
        if is_positive(cleaned):
            state.step = "awaiting_datetime"
            return STORE_SELECTION_MESSAGE.format(city=state.city or "вашем городе", address=state.address or ADDRESS_FALLBACK)
        return "Стажировка каждый день в 8 утра на какой день вас записать? Укажите день недели например: Вторник"

    if state.step == "awaiting_datetime":
        try:
            state.internship_date = resolve_internship_date(text).strftime("%d.%m.%Y")
        except ValueError as exc:
            return str(exc)
        state.date_time = text.strip()
        state.step = "awaiting_full_name"
        return CONFIRMATION_MESSAGE

    if state.step == "awaiting_full_name":
        try:
            state.last_name, state.first_name = split_full_name(text)
        except ValueError as exc:
            return str(exc)
        state.full_name = text.strip()
        try:
            state.phone = normalize_phone(text)
        except ValueError:
            state.phone = None
        if state.phone:
            state.step = "ready_to_submit"
            state.application_status = "pending"
            return ""
        state.step = "awaiting_phone"
        return "И номер"

    if state.step == "awaiting_phone":
        try:
            state.phone = normalize_phone(text)
        except ValueError as exc:
            return str(exc)
        state.step = "ready_to_submit"
        state.application_status = "pending"
        return ""

    return ""


def handle_webhook_event(client: AvitoClient, state: ConversationState, payload: dict[str, Any]) -> dict[str, Any]:
    message_text = _extract_message_text(payload)
    if not message_text:
        return {"status": "ignored"}

    chat_id = _extract_chat_id(payload)
    city_hint = _extract_city(payload)
    reply = handle_user_message(state, message_text, client=client, chat_id=chat_id, city_hint=city_hint)

    if not reply:
        return {"status": "ignored", "chat_id": chat_id}

    if reply == INITIAL_MESSAGE:
        try:
            client.send_message(chat_id, reply)
        except Exception as exc:
            return {"status": "error", "reply": reply, "chat_id": chat_id, "error": str(exc)}
        schedule_delayed_message(client, chat_id, FOLLOW_UP_MESSAGE, delay=5)
    else:
        schedule_delayed_message(client, chat_id, reply, delay=2)

    return {"status": "processed", "reply": reply, "chat_id": chat_id}


def _extract_message_text(payload: dict[str, Any]) -> str | None:
    for value in _walk(payload):
        if isinstance(value, dict):
            for key in ("text", "content"):
                txt = value.get(key)
                if isinstance(txt, str) and txt.strip():
                    return txt
    return None


def _extract_chat_id(payload: dict[str, Any]) -> str:
    for value in _walk(payload):
        if isinstance(value, dict):
            chat_id = value.get("chat_id")
            if isinstance(chat_id, (int, str)) and str(chat_id).strip():
                return str(chat_id)
            chat = value.get("chat")
            if isinstance(chat, dict):
                chat_id = chat.get("id")
                if isinstance(chat_id, (int, str)) and str(chat_id).strip():
                    return str(chat_id)
    return "unknown"


def _extract_city(payload: dict[str, Any]) -> str | None:
    for value in _walk(payload):
        if isinstance(value, str):
            lowered = value.lower()
            if "новороссийск" in lowered:
                return "Новороссийск"
            if "москва" in lowered:
                return "Москва"
            if "санкт-петербург" in lowered:
                return "Санкт-Петербург"
            if "краснодар" in lowered:
                return "Краснодар"
        if isinstance(value, dict):
            city = value.get("city")
            if isinstance(city, str) and city.strip():
                return city
    return None


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def is_positive(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]", "", (text or "").strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if re.search(r"\b(?:нет|отказ|не\s+готов(?:а)?|не\s+соглас(?:ен|на)|не\s+интересно)\b", cleaned):
        return False
    positive_words = {
        "да",
        "согласен",
        "согласна",
        "угу",
        "ок",
        "yes",
        "y",
        "ok",
        "давай",
        "готов",
        "готова",
        "конечно",
        "хорошо",
        "интересно",
    }
    return any(word in positive_words for word in cleaned.split())


def looks_like_datetime(text: str) -> bool:
    try:
        resolve_internship_date(text)
        return True
    except ValueError:
        return False


def normalize_city(city_hint: str | None) -> str | None:
    if not city_hint:
        return None
    name = city_hint.strip()
    if not name:
        return None
    return name


def asks_for_address(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return "адрес" in lowered or "где склад" in lowered or "где находится" in lowered or "склад" in lowered and "где" in lowered


def resolve_address(city: str | None) -> str:
    if not city:
        return ADDRESS_FALLBACK
    lowered = city.strip().lower()
    aliases = {
        "спб": "санкт-петербург",
        "питер": "санкт-петербург",
        "мск": "москва",
        "г. москва": "москва",
        "г. санкт-петербург": "санкт-петербург",
    }
    normalized = aliases.get(lowered, lowered)
    if normalized in ADDRESS_BY_CITY:
        return ADDRESS_BY_CITY[normalized]
    for key, value in ADDRESS_BY_CITY.items():
        if key in normalized:
            return value
    return ADDRESS_FALLBACK


def schedule_delayed_message(client: AvitoClient, chat_id: str, message: str, delay: int = 15) -> None:
    threading.Timer(delay, lambda: _send_delayed_message(client, chat_id, message)).start()


def _send_delayed_message(client: AvitoClient, chat_id: str, message: str) -> None:
    try:
        client.send_message(chat_id, message)
    except Exception:
        pass
