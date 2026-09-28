#!/usr/bin/env python3
"""Личный Telegram-бот: фото еды -> LogMeal -> подтверждение блюда -> КБЖУ и JSON в чате."""

import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

BASE_URL = "https://api.logmeal.com/v2"
SPOONACULAR_URL = "https://api.spoonacular.com"
CONFIG_FILE = Path(__file__).with_name("bot_token.json")
LOGMEAL_TOKEN_FILE = Path(__file__).with_name("token.json")
LEGACY_LOGMEAL_TOKEN_FILE = Path(__file__).parent.parent / "token.json"
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_IMAGE = 950_000
SESSION_TIMEOUT = 5 * 60
LOG = logging.getLogger("telegram_bot")


class LogMealHTTPError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f"LogMeal вернул HTTP {status}: {detail}")
        self.status = status


def load_config():
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"Нет файла токена Telegram: {CONFIG_FILE}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Некорректный JSON в {CONFIG_FILE}: {error.msg}") from error

    if not isinstance(config, dict):
        raise RuntimeError("bot_token.json должен содержать JSON-объект")
    token = config.get("token")
    allowed = config.get("allowed_user_ids", [])
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError('Укажите строковый токен бота в поле "token" файла bot_token.json')
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token.strip()):
        raise RuntimeError(
            "Некорректный формат токена в bot_token.json. Вставьте полный API Token "
            "из @BotFather: цифры, двоеточие и длинная строка. Токен сюда не присылайте."
        )
    if not isinstance(allowed, list) or any(type(user_id) is not int for user_id in allowed):
        raise RuntimeError('Поле "allowed_user_ids" должно быть массивом числовых Telegram ID')
    invite_code = config.get("invite_code", "")
    if not isinstance(invite_code, str):
        raise RuntimeError('Поле "invite_code" должно быть строкой')
    spoonacular_api_key = config.get("spoonacular_api_key", "")
    if not isinstance(spoonacular_api_key, str):
        raise RuntimeError('Поле "spoonacular_api_key" должно быть строкой')
    default_provider = config.get("default_provider", "logmeal")
    if default_provider not in ("logmeal", "spoonacular"):
        raise RuntimeError('Поле "default_provider" должно быть "logmeal" или "spoonacular"')
    admin_id = allowed[0] if allowed else None
    return token.strip(), set(allowed), invite_code, admin_id, spoonacular_api_key.strip(), default_provider


def load_logmeal_token():
    path = LOGMEAL_TOKEN_FILE if LOGMEAL_TOKEN_FILE.is_file() else LEGACY_LOGMEAL_TOKEN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"Нет файла токена LogMeal: {LOGMEAL_TOKEN_FILE}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Некорректный JSON в {path}: {error.msg}") from error
    token = data.get("token") if isinstance(data, dict) else data
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError(f'В {path} укажите строковый токен: {{"token": "ВАШ_ТОКЕН"}}')
    return token.strip()


def post_json(url, token, body, content_type):
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise LogMealHTTPError(error.code, detail) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Ошибка соединения с LogMeal: {error}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("LogMeal вернул ответ не в формате JSON") from error


def telegram_request(token, method, payload=None, *, timeout=45):
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        if error.code in {401, 404}:
            raise RuntimeError(
                f"Telegram {method}: токен в bot_token.json не принят (HTTP {error.code}). "
                "Проверьте токен, выданный @BotFather."
            ) from error
        raise RuntimeError(f"Telegram {method}: HTTP {error.code}: {details}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Telegram {method}: ошибка соединения: {error}") from error
    if not result.get("ok"):
        raise RuntimeError(f'Telegram {method}: {result.get("description", "неизвестная ошибка")}')
    return result["result"]


def download_photo(token, file_id):
    info = telegram_request(token, "getFile", {"file_id": file_id})
    if info.get("file_size", 0) > MAX_DOWNLOAD:
        raise ValueError("Файл больше 20 МБ. Отправьте фото меньшего размера.")
    file_path = info.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram не вернул путь к фото")
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    try:
        with urllib.request.urlopen(url, timeout=45) as response:
            data = response.read(MAX_DOWNLOAD + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError("Не удалось скачать фото из Telegram") from error
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Файл больше 20 МБ. Отправьте фото меньшего размера.")
    return data


def image_type(data):
    """Определить тип файла без преобразования полученных из Telegram байтов."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format == "PNG":
                return "meal.png", "image/png"
            # Некоторые JPEG с iPhone Pillow определяет как MPO.
            if source.format in {"JPEG", "MPO"}:
                return "meal.jpg", "image/jpeg"
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as error:
        raise ValueError("Не удалось прочитать фото. Отправьте JPG или PNG.") from error
    raise ValueError("Отправьте JPG или PNG (можно как обычное фото).")


def prepare_image(data):
    """Сжать фото после HTTP 413, удалив EXIF; оригинал не сохранять."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            # Некоторые JPEG с iPhone Pillow определяет как MPO (несколько кадров).
            # Для распознавания достаточно первого кадра, открытого по умолчанию.
            if source.format not in {"JPEG", "PNG", "MPO"}:
                raise ValueError("Отправьте JPG или PNG (можно как обычное фото).")
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as error:
        raise ValueError("Не удалось прочитать фото. Отправьте JPG или PNG.") from error

    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    for _ in range(8):
        for quality in (85, 75, 65, 55):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            if output.tell() < MAX_IMAGE:
                return output.getvalue()
        if min(image.size) < 300:
            break
        image.thumbnail((int(image.width * 0.8), int(image.height * 0.8)), Image.Resampling.LANCZOS)
    raise ValueError("Не удалось уменьшить фото до лимита LogMeal. Выберите другое фото.")


def upload_recognition(image_data, logmeal_token, filename, mime_type):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("ascii") + image_data + f"\r\n--{boundary}--\r\n".encode("ascii")
    return post_json(
        f"{BASE_URL}/image/segmentation/complete",
        logmeal_token,
        body,
        f"multipart/form-data; boundary={boundary}",
    )


def recognize_image(image_data, logmeal_token, notify=None):
    """Загрузить фото в LogMeal и вернуть результат сегментации (без КБЖУ)."""
    filename, mime_type = image_type(image_data)
    LOG.info("LogMeal: отправляю файл из Telegram без обработки (%s, %d байт)", mime_type, len(image_data))
    if notify:
        notify("Отправил оригинал")
    try:
        recognition = upload_recognition(image_data, logmeal_token, filename, mime_type)
        LOG.info("LogMeal: исходный файл принят")
    except LogMealHTTPError as error:
        if error.status != 413:
            raise
        LOG.info("LogMeal: HTTP 413, сжимаю изображение")
        if notify:
            notify("Сжатие")
        compressed = prepare_image(image_data)
        LOG.info("LogMeal: повторная отправка JPEG (%d байт)", len(compressed))
        recognition = upload_recognition(compressed, logmeal_token, "meal.jpg", "image/jpeg")
        LOG.info("LogMeal: сжатое фото принято")
        if notify:
            notify("Отправлено сжатое фото")

    image_id = recognition.get("imageId")
    if type(image_id) is not int:
        raise RuntimeError("LogMeal не вернул imageId для загруженного фото")
    items = recognition.get("segmentation_results") or []
    LOG.info("LogMeal: imageId=%s, обнаружено областей: %d", image_id, len(items))
    return image_id, items


def confirm_dish(logmeal_token, image_id, confirmed):
    """Подтвердить блюда через POST /v2/image/confirm/dish.

    confirmed: list of (food_item_position, dish_id) — только подтверждённые области.
    """
    if not confirmed:
        return
    payload = {
        "imageId": image_id,
        "confirmedClass": [dish_id for _, dish_id in confirmed],
        "source": ["logmeal"] * len(confirmed),
        "food_item_position": [pos for pos, _ in confirmed],
    }
    LOG.info("LogMeal: confirm/dish imageId=%s, подтверждено областей: %d", image_id, len(confirmed))
    return post_json(
        f"{BASE_URL}/image/confirm/dish",
        logmeal_token,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )


def confirm_quantity(logmeal_token, image_id, quantity_dict):
    """Подтвердить вес порций через POST /v2/nutrition/confirm/quantity.

    quantity_dict: {food_item_position: grams, ...}
    """
    payload = {"imageId": image_id, "quantity": quantity_dict}
    LOG.info("LogMeal: confirm/quantity imageId=%s, порции: %s", image_id, quantity_dict)
    return post_json(
        f"{BASE_URL}/nutrition/confirm/quantity",
        logmeal_token,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )


def fetch_nutrition(logmeal_token, image_id):
    """Получить КБЖУ после подтверждения блюда и порции."""
    nutrition = post_json(
        f"{BASE_URL}/nutrition/recipe/nutritionalInfo",
        logmeal_token,
        json.dumps({"imageId": image_id}).encode("utf-8"),
        "application/json",
    )
    LOG.info("LogMeal: КБЖУ получено для imageId=%s, калории=%s, расчётная порция=%s г",
             image_id, (nutrition.get("nutritional_info") or {}).get("calories"), nutrition.get("serving_size"))
    return nutrition


def format_nutrient(nutrients, code):
    value = nutrients.get(code, {})
    if not isinstance(value, dict) or not isinstance(value.get("quantity"), (int, float)):
        return "нет данных"
    return f'{value["quantity"]:.1f} {value.get("unit", "г")}'


def format_result(image_id, nutrition, region_count=1):
    lines = [f"Результат LogMeal · фото #{image_id}"]

    if region_count > 1:
        lines.append(f"⚠️ Подтверждено {region_count} областей. Разбивка по областям:")
        for item in (nutrition.get("nutritional_info_per_item") or [])[:10]:
            details = item.get("nutritional_info") or {}
            size = item.get("serving_size")
            calories_per_item = details.get("calories")
            if isinstance(size, (int, float)) and isinstance(calories_per_item, (int, float)):
                lines.append(f'#{item.get("food_item_position", "?")}: {size:g} г, {calories_per_item:.1f} ккал')

    info = nutrition.get("nutritional_info")
    if not nutrition.get("hasNutritionalInfo") or not isinstance(info, dict):
        lines.append("КБЖУ для этого фото не рассчитано. Подробности — в JSON-файле ниже.")
        return "\n".join(lines)

    nutrients = info.get("totalNutrients") or {}
    calories = info.get("calories")
    if isinstance(calories, (int, float)):
        lines.append(f"Калории: {calories:.1f} ккал")
    lines.extend(
        [
            f'Белки: {format_nutrient(nutrients, "PROCNT")}',
            f'Жиры: {format_nutrient(nutrients, "FAT")}',
            f'Углеводы: {format_nutrient(nutrients, "CHOCDF")}',
        ]
    )
    serving_size = nutrition.get("serving_size")
    if isinstance(serving_size, (int, float)):
        lines.append(f"Порция: {serving_size:g} г")
    return "\n".join(lines)


class SpoonacularHTTPError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f"Spoonacular вернул HTTP {status}: {detail}")
        self.status = status


def spoonacular_get(api_key, endpoint, params=None):
    query = dict(params or {})
    query["apiKey"] = api_key
    encoded = urllib.parse.urlencode(query, doseq=True)
    url = f"{SPOONACULAR_URL}{endpoint}?{encoded}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SpoonacularHTTPError(error.code, detail) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Ошибка соединения с Spoonacular: {error}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Spoonacular вернул ответ не в формате JSON") from error


def spoonacular_analyze_image(api_key, image_data, filename="meal.jpg", mime_type="image/jpeg"):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("ascii") + image_data + f"\r\n--{boundary}--\r\n".encode("ascii")
    url = f"{SPOONACULAR_URL}/recipes/analyze/image?apiKey={api_key}"
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SpoonacularHTTPError(error.code, detail) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Ошибка соединения с Spoonacular: {error}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Spoonacular вернул ответ не в формате JSON") from error


def spoonacular_recipe_nutrition(api_key, recipe_id):
    return spoonacular_get(api_key, f"/recipes/{recipe_id}/information", {"includeNutrition": "true"})


def format_spoonacular_result(recipe_info):
    title = recipe_info.get("title", "неизвестно")
    lines = [f"Spoonacular · {title}"]
    nutrition = recipe_info.get("nutrition") or {}
    nutrients = nutrition.get("nutrients") or []
    nutrient_map = {n.get("name"): n for n in nutrients if isinstance(n, dict)}
    calories_info = nutrient_map.get("Calories") or {}
    calories = calories_info.get("amount")
    if isinstance(calories, (int, float)):
        lines.append(f"Калории: {calories:.1f} ккал")
    protein_info = nutrient_map.get("Protein") or {}
    protein = protein_info.get("amount")
    if isinstance(protein, (int, float)):
        lines.append(f"Белки: {protein:.1f} г")
    fat_info = nutrient_map.get("Fat") or {}
    fat = fat_info.get("amount")
    if isinstance(fat, (int, float)):
        lines.append(f"Жиры: {fat:.1f} г")
    carbs_info = nutrient_map.get("Carbohydrates") or {}
    carbs = carbs_info.get("amount")
    if isinstance(carbs, (int, float)):
        lines.append(f"Углеводы: {carbs:.1f} г")
    servings = recipe_info.get("servings")
    if isinstance(servings, (int, float)):
        lines.append(f"Порции: {servings:g}")
    return "\n".join(lines)


def send_document(token, chat_id, result):
    boundary = uuid.uuid4().hex
    data = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="document"; filename="logmeal_response.json"\r\n'
        "Content-Type: application/json\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            reply = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError("Не удалось отправить JSON-файл в Telegram") from error
    if not reply.get("ok"):
        raise RuntimeError("Telegram отклонил отправку JSON-файла")


def _top3(item):
    return (item.get("recognition_results") or [])[:3]


def _dish_label(prediction):
    name = prediction.get("name", "неизвестно")
    prob = prediction.get("prob")
    if isinstance(prob, (int, float)):
        return f"{name} ({prob:.0%})"
    return name


def _answer_callback(token, callback_id, text=""):
    telegram_request(token, "answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text,
    })


def _build_toggle_keyboard(image_id, items, confirmed):
    """Построить toggle-клавиатуру: каждая область — отдельная строка с чекбоксом."""
    rows = []
    for item in items:
        pos = item.get("food_item_position", 1)
        predictions = _top3(item)
        best = predictions[0] if predictions else {}
        dish_id = best.get("id", 0)
        name = best.get("name", "?")
        prob = best.get("prob")
        label = f"#{pos} {name}"
        if isinstance(prob, (int, float)):
            label += f" {prob:.0%}"
        if pos in confirmed:
            rows.append([{
                "text": f"☑ {label}",
                "callback_data": f"toggle:{image_id}:{pos}:{dish_id}",
            }])
        else:
            rows.append([{
                "text": f"☐ {label}",
                "callback_data": f"toggle:{image_id}:{pos}:{dish_id}",
            }])
    has_confirmed = bool(confirmed)
    if has_confirmed:
        rows.append([{
            "text": "Отправить",
            "callback_data": f"send:{image_id}",
        }])
    return {"inline_keyboard": rows}


def _send_dish_buttons(token, chat_id, image_id, items):
    """Отправить сообщение с toggle-кнопками для каждой области."""
    confirmed = {}
    lines = [f"LogMeal нашёл {len(items)} областей. Нажмите, чтобы выбрать:"]
    for item in items:
        pos = item.get("food_item_position", 1)
        predictions = _top3(item)
        best = predictions[0] if predictions else {}
        lines.append(f"  #{pos}: {_dish_label(best)}")
    text = "\n".join(lines)
    reply_markup = _build_toggle_keyboard(image_id, items, confirmed)
    result = telegram_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": reply_markup,
    })
    return result.get("message_id")


def _update_toggle_keyboard(token, chat_id, session, image_id):
    """Обновить toggle-клавиатуру после клика."""
    msg_id = session.get("buttons_msg_id")
    if not msg_id:
        return
    items = session.get("items", [])
    confirmed = session.get("confirmed", {})
    reply_markup = _build_toggle_keyboard(image_id, items, confirmed)
    try:
        telegram_request(token, "editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "reply_markup": reply_markup,
        })
    except RuntimeError:
        pass


def _ask_weight(token, chat_id, image_id, serving_size, region_count):
    """Спросить вес; Enter = стандартная порция."""
    if isinstance(serving_size, (int, float)):
        hint = f"Стандартная порция: {serving_size:g} г."
    else:
        hint = "Стандартная порция неизвестна."
    if region_count == 1:
        prompt = f"{hint}\nВведите вес в граммах или нажмите Enter — оставим стандартную."
    else:
        prompt = f"{hint}\nВведите вес для каждой области: 1=150 2=80\nИли Enter — оставим стандартные порции."
    telegram_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": prompt,
        "reply_markup": {"inline_keyboard": [[{
            "text": "Стандартная порция",
            "callback_data": f"weight:{image_id}:skip",
        }]]},
    })


def _finish_with_nutrition(token, chat_id, logmeal_token, image_id, region_count):
    try:
        nutrition = fetch_nutrition(logmeal_token, image_id)
    except (LogMealHTTPError, RuntimeError) as error:
        LOG.warning("Не удалось получить КБЖУ: imageId=%s, ошибка=%s", image_id, error)
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Не удалось получить КБЖУ: {error}"})
        return
    text = format_result(image_id, nutrition, region_count)
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})
    send_document(token, chat_id, {"imageId": image_id, "nutrition": nutrition})
    LOG.info("Ответ и JSON отправлены: chat_id=%s, imageId=%s", chat_id, image_id)


def _build_spoon_recipe_keyboard(recipes):
    rows = []
    for recipe in recipes[:5]:
        rid = recipe.get("id", 0)
        title = recipe.get("title", "?")
        confidence = recipe.get("confidence")
        label = title
        if isinstance(confidence, (int, float)):
            label += f" ({confidence:.0%})"
        rows.append([{
            "text": label,
            "callback_data": f"spoon:{rid}",
        }])
    return {"inline_keyboard": rows}


def _build_main_keyboard():
    return {"inline_keyboard": [[{
        "text": "\u2699\ufe0f Настройки",
        "callback_data": "menu:settings",
    }]]}


def _build_settings_keyboard(provider, is_admin):
    rows = [[{
        "text": "\U0001f504 Сменить провайдер",
        "callback_data": "menu:provider",
    }]]
    if is_admin:
        rows.append([{
            "text": "\U0001f465 Пользователи",
            "callback_data": "menu:users",
        }])
    rows.append([{
        "text": "\u25c0\ufe0f Назад",
        "callback_data": "menu:main",
    }])
    return {"inline_keyboard": rows}


def _build_provider_keyboard(current_provider, spoon_key_available):
    rows = []
    if current_provider == "logmeal":
        rows.append([{
            "text": "\u2705 LogMeal",
            "callback_data": "noop",
        }])
    else:
        rows.append([{
            "text": "LogMeal",
            "callback_data": "menu:set_provider:logmeal",
        }])
    if spoon_key_available:
        if current_provider == "spoonacular":
            rows.append([{
                "text": "\u2705 Spoonacular",
                "callback_data": "noop",
            }])
        else:
            rows.append([{
                "text": "Spoonacular",
                "callback_data": "menu:set_provider:spoonacular",
            }])
    rows.append([{
        "text": "\u25c0\ufe0f Назад",
        "callback_data": "menu:settings",
    }])
    return {"inline_keyboard": rows}


def _build_users_keyboard(users, admin_id):
    rows = [[{
        "text": "\u2795 Добавить",
        "callback_data": "menu:invite",
    }]]
    rows.append([{
        "text": "\u25c0\ufe0f Назад",
        "callback_data": "menu:settings",
    }])
    return {"inline_keyboard": rows}


def _edit_menu_message(token, chat_id, msg_id, text, keyboard):
    try:
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": text,
            "reply_markup": keyboard,
        })
    except RuntimeError:
        pass


def _handle_menu_callback(token, logmeal_token, spoon_key, default_provider, allowed_ids, admin_id,
                          chat_id, user_id, callback_id, data):
    session = _session(user_id)
    msg_id = session.get("menu_msg_id")
    provider = session.get("provider") or default_provider
    is_admin = user_id == admin_id
    parts = data.split(":")

    if parts[1] == "main":
        _answer_callback(token, callback_id)
        text = (
            "\U0001f957 Бот КБЖУ\n\n"
            "Отправьте фото еды — я определю блюдо и посчитаю калории, белки, жиры, углеводы."
        )
        _edit_menu_message(token, chat_id, msg_id, text, _build_main_keyboard())

    elif parts[1] == "settings":
        _answer_callback(token, callback_id)
        label = "LogMeal" if provider == "logmeal" else "Spoonacular"
        text = f"\u2699\ufe0f Настройки\n\nПровайдер: {label}"
        _edit_menu_message(token, chat_id, msg_id, text, _build_settings_keyboard(provider, is_admin))

    elif parts[1] == "provider":
        _answer_callback(token, callback_id)
        text = (
            "\U0001f504 Выберите провайдер\n\n"
            "LogMeal — сегментация + подтверждение блюда и порции\n"
            "Spoonacular — распознавание + выбор из вариантов"
        )
        _edit_menu_message(token, chat_id, msg_id, text,
                           _build_provider_keyboard(provider, bool(spoon_key)))

    elif parts[1] == "set_provider" and len(parts) >= 3:
        choice = parts[2]
        if choice not in ("logmeal", "spoonacular"):
            _answer_callback(token, callback_id, "?")
            return
        if choice == "spoonacular" and not spoon_key:
            _answer_callback(token, callback_id, "Сначала добавьте API-ключ Spoonacular в bot_token.json")
            return
        session["provider"] = choice
        _save_provider_default(choice)
        label = "LogMeal" if choice == "logmeal" else "Spoonacular"
        LOG.info("Пользователь %d переключил провайдер на %s (меню)", user_id, choice)
        _answer_callback(token, callback_id, f"Провайдер: {label}")
        text = (
            "\U0001f504 Выберите провайдер\n\n"
            "LogMeal — сегментация + подтверждение блюда и порции\n"
            "Spoonacular — распознавание + выбор из вариантов"
        )
        _edit_menu_message(token, chat_id, msg_id, text,
                           _build_provider_keyboard(choice, bool(spoon_key)))

    elif parts[1] == "users":
        if not is_admin:
            _answer_callback(token, callback_id, "Только для админа")
            return
        _answer_callback(token, callback_id)
        lines = ["\U0001f465 Пользователи с доступом\n"]
        for uid in sorted(allowed_ids):
            lines.append(f"\u2022 {uid} (админ)" if uid == admin_id else f"\u2022 {uid}")
        text = "\n".join(lines)
        _edit_menu_message(token, chat_id, msg_id, text, _build_users_keyboard(allowed_ids, admin_id))

    elif parts[1] == "invite":
        if not is_admin:
            _answer_callback(token, callback_id, "Только для админа")
            return
        _answer_callback(token, callback_id)
        session["state"] = "awaiting_invite_id"
        _edit_menu_message(token, chat_id, msg_id,
                           "Введите Telegram ID пользователя для добавления:",
                           {"inline_keyboard": [[{
                               "text": "\u25c0\ufe0f Отмена",
                               "callback_data": "menu:users",
                           }]]})

    else:
        _answer_callback(token, callback_id, "?")


def handle_photo_spoonacular(token, spoon_key, chat_id, user_id, file_id):
    session = _session(user_id)
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Получил фото, анализирую через Spoonacular…"})
    try:
        raw = download_photo(token, file_id)
        LOG.info("Фото скачано для Spoonacular: chat_id=%s, размер=%d байт", chat_id, len(raw))
        filename, mime_type = image_type(raw)
    except (ValueError, RuntimeError) as error:
        LOG.warning("Не удалось прочитать фото: chat_id=%s, ошибка=%s", chat_id, error)
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Не удалось обработать фото: {error}"})
        return

    try:
        result = spoonacular_analyze_image(spoon_key, raw, filename, mime_type)
    except (SpoonacularHTTPError, RuntimeError) as error:
        LOG.warning("Spoonacular analyse_image не удался: %s", error)
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Не удалось распознать фото: {error}"})
        return

    recipes = result.get("recipes") or []
    if not recipes:
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Spoonacular не нашёл блюд на фото. Попробуйте другое фото."})
        return

    session["spoon_recipes"] = recipes
    session["state"] = "awaiting_spoon_recipe"

    if len(recipes) == 1:
        rid = recipes[0].get("id", 0)
        _fetch_and_send_spoon_nutrition(token, spoon_key, chat_id, user_id, rid)
        return

    lines = [f"Spoonacular нашёл {len(recipes)} вариантов. Выберите:"]
    for recipe in recipes[:5]:
        title = recipe.get("title", "?")
        confidence = recipe.get("confidence")
        if isinstance(confidence, (int, float)):
            lines.append(f"  • {title} ({confidence:.0%})")
        else:
            lines.append(f"  • {title}")
    text = "\n".join(lines)
    reply_markup = _build_spoon_recipe_keyboard(recipes)
    msg = telegram_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": reply_markup,
    })
    session["spoon_buttons_msg_id"] = msg.get("message_id")


def _fetch_and_send_spoon_nutrition(token, spoon_key, chat_id, user_id, recipe_id):
    session = _session(user_id)
    session["state"] = "idle"
    try:
        recipe_info = spoonacular_recipe_nutrition(spoon_key, recipe_id)
    except (SpoonacularHTTPError, RuntimeError) as error:
        LOG.warning("Spoonacular recipe_nutrition не удался: %s", error)
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Не удалось получить КБЖУ: {error}"})
        return
    text = format_spoonacular_result(recipe_info)
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})
    send_document(token, chat_id, {"source": "spoonacular", "recipeId": recipe_id, "recipe": recipe_info})
    LOG.info("Spoonacular ответ отправлен: chat_id=%s, recipeId=%s", chat_id, recipe_id)


def _clear_spoon_recipe_buttons(token, chat_id, session):
    msg_id = session.get("spoon_buttons_msg_id")
    if not msg_id:
        return
    try:
        telegram_request(token, "editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "reply_markup": {"inline_keyboard": []},
        })
    except RuntimeError:
        pass


sessions = {}


def _session(user_id):
    now = time.monotonic()
    session = sessions.get(user_id)
    if session and now - session.get("ts", 0) > SESSION_TIMEOUT:
        del sessions[user_id]
        session = None
    if not session:
        session = {"ts": now}
        sessions[user_id] = session
    session["ts"] = now
    return session


def handle_photo(token, logmeal_token, chat_id, user_id, file_id):
    """Обработать фото: распознать и показать варианты блюд."""
    session = _session(user_id)
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Получил фото, начинаю анализ…"})
    try:
        raw = download_photo(token, file_id)
        LOG.info("Фото скачано: chat_id=%s, размер=%d байт", chat_id, len(raw))
        image_id, items = recognize_image(
            raw, logmeal_token,
            notify=lambda text: telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text}),
        )
    except (ValueError, RuntimeError) as error:
        LOG.warning("Не удалось обработать фото: chat_id=%s, ошибка=%s", chat_id, error)
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Не удалось обработать фото: {error}"})
        return

    session["image_id"] = image_id
    session["items"] = items
    session["confirmed"] = {}
    session["state"] = "awaiting_dish"

    if len(items) == 1:
        item = items[0]
        predictions = _top3(item)
        pos = item.get("food_item_position", 1)
        dish_id = predictions[0].get("id", 0) if predictions else 0
        session["confirmed"][pos] = dish_id
        _auto_confirm_single(token, logmeal_token, chat_id, user_id, image_id, pos, dish_id)
    else:
        msg_id = _send_dish_buttons(token, chat_id, image_id, items)
        session["buttons_msg_id"] = msg_id


def _auto_confirm_single(token, logmeal_token, chat_id, user_id, image_id, pos, dish_id):
    """Автоподтверждение единственного варианта, сразу переход к весу."""
    try:
        confirm_dish(logmeal_token, image_id, [(pos, dish_id)])
    except (LogMealHTTPError, RuntimeError) as error:
        LOG.warning("confirm/dish не удался: %s", error)
    session = _session(user_id)
    session["state"] = "awaiting_weight"
    serving = None
    try:
        nutrition = fetch_nutrition(logmeal_token, image_id)
        serving = nutrition.get("serving_size")
    except (LogMealHTTPError, RuntimeError):
        pass
    _ask_weight(token, chat_id, image_id, serving, 1)


def _clear_dish_buttons(token, chat_id, session):
    """Убрать inline-кнопки, когда все области обработаны."""
    msg_id = session.get("buttons_msg_id")
    if not msg_id:
        return
    try:
        telegram_request(token, "editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "reply_markup": {"inline_keyboard": []},
        })
    except RuntimeError:
        pass


def handle_callback(token, logmeal_token, spoon_key, default_provider, chat_id, user_id, callback_id, data):
    """Обработать нажатие inline-кнопки."""
    try:
        _handle_callback_inner(token, logmeal_token, chat_id, user_id, callback_id, data)
    except Exception as error:
        LOG.error("Ошибка в handle_callback: %s", error)
        try:
            _answer_callback(token, callback_id, f"Ошибка: {error}")
        except Exception:
            pass


def _handle_callback_inner(token, logmeal_token, chat_id, user_id, callback_id, data):
    session = _session(user_id)
    parts = data.split(":")

    if parts[0] == "toggle" and len(parts) >= 4:
        image_id = int(parts[1])
        pos = int(parts[2]) if parts[2].isdigit() else parts[2]
        dish_id = int(parts[3])
        confirmed = session.get("confirmed", {})
        if pos in confirmed:
            confirmed.pop(pos)
            _answer_callback(token, callback_id, f"☐ #{pos} снят")
        else:
            confirmed[pos] = dish_id
            dish_name = "?"
            for item in session.get("items", []):
                if item.get("food_item_position") == pos:
                    for pred in _top3(item):
                        if pred.get("id") == dish_id:
                            dish_name = pred.get("name", "?")
                            break
                    break
            _answer_callback(token, callback_id, f"☑ #{pos} {dish_name}")
        session["confirmed"] = confirmed
        _update_toggle_keyboard(token, chat_id, session, image_id)

    elif parts[0] == "send" and len(parts) >= 2:
        image_id = int(parts[1])
        _answer_callback(token, callback_id)
        _clear_dish_buttons(token, chat_id, session)
        _proceed_after_dish_confirm(token, logmeal_token, chat_id, user_id, image_id)

    elif parts[0] == "weight" and len(parts) >= 3:
        image_id = int(parts[1])
        action = parts[2]
        _answer_callback(token, callback_id)
        if action == "skip":
            session["state"] = "idle"
            region_count = len(session.get("confirmed", {}))
            _finish_with_nutrition(token, chat_id, logmeal_token, image_id, region_count)

    elif parts[0] == "spoon" and len(parts) >= 2:
        recipe_id = int(parts[1])
        _answer_callback(token, callback_id)
        _clear_spoon_recipe_buttons(token, chat_id, session)
        _fetch_and_send_spoon_nutrition(token, spoon_key, chat_id, user_id, recipe_id)

    elif parts[0] == "provider" and len(parts) >= 2:
        choice = parts[1]
        if choice in ("logmeal", "spoonacular"):
            if choice == "spoonacular" and not spoon_key:
                _answer_callback(token, callback_id, "Сначала добавьте API-ключ Spoonacular в bot_token.json")
                return
            session["provider"] = choice
            _save_provider_default(choice)
            label = "LogMeal" if choice == "logmeal" else "Spoonacular"
            LOG.info("Пользователь %d переключил провайдер на %s (callback)", user_id, choice)
            _answer_callback(token, callback_id, f"Провайдер: {label}")
        else:
            _answer_callback(token, callback_id, "?")
    elif data == "noop":
        _answer_callback(token, callback_id)
    else:
        _answer_callback(token, callback_id, "?")


def _proceed_after_dish_confirm(token, logmeal_token, chat_id, user_id, image_id):
    """Все области подтверждены или отклонены — подтвердить блюда и спросить вес."""
    session = _session(user_id)
    confirmed = session.get("confirmed", {})
    if not confirmed:
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Все области отклонены. Отправьте новое фото."})
        session["state"] = "idle"
        return

    confirmed_list = [(pos, dish_id) for pos, dish_id in confirmed.items()]
    confirm_ok = True
    try:
        confirm_dish(logmeal_token, image_id, confirmed_list)
    except LogMealHTTPError as error:
        confirm_ok = False
        if error.status == 403:
            LOG.warning("confirm/dish: тариф LogMeal не поддерживает подтверждение блюд (403)")
            telegram_request(token, "sendMessage", {
                "chat_id": chat_id,
                "text": "⚠️ Тариф LogMeal не поддерживает подтверждение блюд. КБЖУ — по стандартным порциям.",
            })
        else:
            LOG.warning("confirm/dish не удался: %s", error)
            telegram_request(token, "sendMessage", {
                "chat_id": chat_id,
                "text": f"Не удалось подтвердить блюдо (HTTP {error.status}). КБЖУ — по стандартным порциям.",
            })
    except RuntimeError as error:
        confirm_ok = False
        LOG.warning("confirm/dish не удался: %s", error)

    session["state"] = "awaiting_weight"
    serving = None
    try:
        nutrition = fetch_nutrition(logmeal_token, image_id)
        serving = nutrition.get("serving_size")
    except (LogMealHTTPError, RuntimeError):
        pass
    region_count = len(confirmed)
    if confirm_ok and region_count == 1:
        _ask_weight(token, chat_id, image_id, serving, region_count)
    elif confirm_ok:
        _ask_weight(token, chat_id, image_id, serving, region_count)
    else:
        telegram_request(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "Показываю КБЖУ по стандартным порциям (без подтверждения).",
        })
        session["state"] = "idle"
        _finish_with_nutrition(token, chat_id, logmeal_token, image_id, region_count)


def _save_provider_default(provider):
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    config["default_provider"] = provider
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def handle_text(token, logmeal_token, spoon_key, default_provider, chat_id, user_id, text, allowed_ids, admin_id):
    """Обработать текстовое сообщение (команды или ввод веса)."""
    session = _session(user_id)

    if user_id == admin_id:
        if text.startswith("/invite "):
            try:
                target_id = int(text.split(maxsplit=1)[1].strip())
            except (ValueError, IndexError):
                telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Формат: /invite <telegram_id>"})
                return
            if target_id in allowed_ids:
                telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Пользователь {target_id} уже имеет доступ."})
                return
            allowed_ids.add(target_id)
            _save_allowed_ids(allowed_ids)
            LOG.info("Админ добавил пользователя %d", target_id)
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Добавлен: {target_id}"})
            return

        if text.startswith("/revoke "):
            try:
                target_id = int(text.split(maxsplit=1)[1].strip())
            except (ValueError, IndexError):
                telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Формат: /revoke <telegram_id>"})
                return
            if target_id == admin_id:
                telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Нельзя отозвать доступ у админа."})
                return
            if target_id not in allowed_ids:
                telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Пользователь {target_id} не в списке."})
                return
            allowed_ids.discard(target_id)
            _save_allowed_ids(allowed_ids)
            LOG.info("Админ отозвал доступ у %d", target_id)
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Доступ отозван: {target_id}"})
            return

        if text == "/list_users":
            lines = [f"• {uid} (админ)" if uid == admin_id else f"• {uid}" for uid in sorted(allowed_ids)]
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Пользователи с доступом:\n" + "\n".join(lines)})
            return

    if text.startswith("/provider"):
        session = _session(user_id)
        parts = text.split()
        if len(parts) == 1:
            current = session.get("provider") or default_provider
            telegram_request(token, "sendMessage", {
                "chat_id": chat_id,
                "text": f"Текущий провайдер: {current}. Переключить: /provider spoonacular или /provider logmeal",
            })
            return
        choice = parts[1].strip().lower()
        if choice not in ("logmeal", "spoonacular"):
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Доступные провайдеры: logmeal, spoonacular"})
            return
        if choice == "spoonacular" and not spoon_key:
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Сначала добавьте API-ключ Spoonacular в bot_token.json (поле spoonacular_api_key)."})
            return
        session["provider"] = choice
        _save_provider_default(choice)
        label = "LogMeal" if choice == "logmeal" else "Spoonacular"
        LOG.info("Пользователь %d переключил провайдер на %s", user_id, choice)
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Провайдер: {label}"})
        return

    if text.startswith("/start") or text.startswith("/help"):
        current_provider = session.get("provider") or default_provider
        label = "LogMeal" if current_provider == "logmeal" else "Spoonacular"
        help_text = (
            "\U0001f957 Бот КБЖУ\n\n"
            "Отправьте фото еды — я определю блюдо и посчитаю калории, белки, жиры, углеводы."
        )
        if user_id == admin_id:
            help_text += "\n\nКоманды админа: /invite, /revoke, /list_users"
        msg = telegram_request(token, "sendMessage", {
            "chat_id": chat_id,
            "text": help_text,
            "reply_markup": _build_main_keyboard(),
        })
        session["menu_msg_id"] = msg.get("message_id")
        return

    if session.get("state") != "awaiting_weight":
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Отправьте фото еды."})
        return

    image_id = session.get("image_id")
    if not image_id:
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Сессия устарела. Отправьте фото заново."})
        session["state"] = "idle"
        return

    confirmed = session.get("confirmed", {})
    region_count = len(confirmed)

    stripped = text.strip()
    if not stripped:
        session["state"] = "idle"
        _finish_with_nutrition(token, chat_id, logmeal_token, image_id, region_count)
        return

    if region_count == 1:
        try:
            grams = float(stripped)
            if grams <= 0:
                raise ValueError
        except ValueError:
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "Введите положительное число граммов или нажмите Enter."})
            return
        pos = next(iter(confirmed))
        try:
            confirm_quantity(logmeal_token, image_id, {str(pos): grams})
        except (LogMealHTTPError, RuntimeError) as error:
            LOG.warning("confirm/quantity не удался: %s", error)
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Не удалось установить вес: {error}"})
            return
    else:
        try:
            qty = {}
            for part in stripped.split():
                key, _, val = part.partition("=")
                qty[key] = float(val)
            if not qty:
                raise ValueError
        except ValueError:
            telegram_request(token, "sendMessage", {
                "chat_id": chat_id,
                "text": "Формат: 1=150 2=80 (номер области=граммы) или Enter для стандартных порций.",
            })
            return
        try:
            confirm_quantity(logmeal_token, image_id, qty)
        except (LogMealHTTPError, RuntimeError) as error:
            LOG.warning("confirm/quantity не удался: %s", error)
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": f"Не удалось установить вес: {error}"})
            return

    session["state"] = "idle"
    _finish_with_nutrition(token, chat_id, logmeal_token, image_id, region_count)


def _save_allowed_ids(allowed_ids):
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    config["allowed_user_ids"] = sorted(allowed_ids)
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def _check_access(token, allowed_ids, user_id, chat_id):
    if user_id in allowed_ids:
        return True
    LOG.warning("Отказано в доступе: user_id=%s", user_id)
    telegram_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": f"Ваш Telegram ID: {user_id}. Для доступа попросите инвайт-ссылку у владельца бота.",
    })
    return False


def main():
    token, allowed_ids, invite_code, admin_id, spoon_key, default_provider = load_config()
    logmeal_token = load_logmeal_token()
    bot = telegram_request(token, "getMe")
    bot_username = bot.get("username")
    LOG.info("Бот @%s запущен. Провайдер по умолчанию: %s. Ожидаю сообщения; остановка: Ctrl+C.", bot_username, default_provider)
    if not allowed_ids:
        LOG.warning("Список allowed_user_ids пуст. Отправьте боту /start, узнайте ID и добавьте его в bot_token.json.")
    if invite_code:
        LOG.info("Инвайт-ссылка: https://t.me/%s?start=%s", bot_username, invite_code)
    if spoon_key:
        LOG.info("Spoonacular API-ключ загружен")
    else:
        LOG.info("Spoonacular API-ключ не задан — /provider spoonacular будет недоступен")
    offset = None
    while True:
        try:
            updates = telegram_request(token, "getUpdates", {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            }, timeout=40)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        cq = update["callback_query"]
                        chat_id = cq["message"]["chat"]["id"]
                        user_id = cq["from"]["id"]
                        if _check_access(token, allowed_ids, user_id, chat_id):
                            handle_callback(token, logmeal_token, spoon_key, default_provider, chat_id, user_id,
                                            cq["id"], cq.get("data", ""))
                        else:
                            try:
                                _answer_callback(token, cq["id"], "Нет доступа")
                            except Exception:
                                pass
                    elif "message" in update:
                        message = update["message"]
                        if message.get("chat", {}).get("type") != "private":
                            continue
                        user_id = message.get("from", {}).get("id")
                        chat_id = message["chat"]["id"]
                        msg_text = message.get("text", "")
                        if msg_text.startswith("/start ") and invite_code and user_id not in allowed_ids:
                            code = msg_text.split(maxsplit=1)[1].strip()
                            if code == invite_code:
                                allowed_ids.add(user_id)
                                _save_allowed_ids(allowed_ids)
                                LOG.info("Пользователь %d добавлен по инвайт-коду", user_id)
                                telegram_request(token, "sendMessage", {
                                    "chat_id": chat_id,
                                    "text": "Добро пожаловать! Отправьте фото еды — я распознаю блюдо, предложу подтвердить и укажу вес порции для точного КБЖУ.",
                                })
                                continue
                        if not _check_access(token, allowed_ids, user_id, chat_id):
                            continue

                        photos = message.get("photo") or []
                        document = message.get("document") or {}
                        if photos:
                            file_id = photos[-1]["file_id"]
                            provider = _session(user_id).get("provider") or default_provider
                            if provider == "spoonacular" and spoon_key:
                                handle_photo_spoonacular(token, spoon_key, chat_id, user_id, file_id)
                            else:
                                handle_photo(token, logmeal_token, chat_id, user_id, file_id)
                        elif document.get("mime_type") in {"image/jpeg", "image/png"}:
                            file_id = document["file_id"]
                            provider = _session(user_id).get("provider") or default_provider
                            if provider == "spoonacular" and spoon_key:
                                handle_photo_spoonacular(token, spoon_key, chat_id, user_id, file_id)
                            else:
                                handle_photo(token, logmeal_token, chat_id, user_id, file_id)
                        elif message.get("text"):
                            handle_text(token, logmeal_token, spoon_key, default_provider, chat_id, user_id, message["text"], allowed_ids, admin_id)
                except (RuntimeError, ValueError) as error:
                    LOG.error("Не удалось ответить пользователю: %s", error)
        except RuntimeError as error:
            LOG.error("Связь с Telegram: %s; повтор через 5 секунд", error)
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        LOG.info("Бот остановлен")
    except (RuntimeError, OSError) as error:
        LOG.error("Не удалось запустить бота: %s", error)
        raise SystemExit(1) from error
