"""Офлайн-проверка распознавания, подтверждения блюда/порции и форматирования."""

import io
import json
import unittest
from unittest.mock import MagicMock, call, patch

from PIL import Image

import telegram_bot as bot


def png_image():
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(output, format="PNG")
    return output.getvalue()


class RecognizeTests(unittest.TestCase):
    def setUp(self):
        self.image = png_image()
        self.recognition = {"imageId": 123, "segmentation_results": []}

    @patch.object(bot, "upload_recognition")
    def test_original_is_sent_unchanged(self, upload):
        upload.return_value = self.recognition
        statuses = []

        image_id, items = bot.recognize_image(self.image, "test-token", statuses.append)

        upload.assert_called_once_with(self.image, "test-token", "meal.png", "image/png")
        self.assertEqual(image_id, 123)
        self.assertEqual(items, [])
        self.assertEqual(statuses, ["Отправил оригинал"])

    @patch.object(bot, "upload_recognition")
    def test_413_retries_once_with_compressed_jpeg(self, upload):
        upload.side_effect = [bot.LogMealHTTPError(413, "too large"), self.recognition]
        statuses = []

        bot.recognize_image(self.image, "test-token", statuses.append)

        self.assertEqual(upload.call_count, 2)
        self.assertEqual(upload.call_args_list[0], call(self.image, "test-token", "meal.png", "image/png"))
        compressed, _, filename, mime = upload.call_args.args
        self.assertEqual((filename, mime), ("meal.jpg", "image/jpeg"))
        self.assertLess(len(compressed), 1_048_576)
        self.assertEqual(Image.open(io.BytesIO(compressed)).format, "JPEG")
        self.assertEqual(statuses, ["Отправил оригинал", "Сжатие", "Отправлено сжатое фото"])

    @patch.object(bot, "upload_recognition")
    def test_other_http_errors_do_not_retry(self, upload):
        upload.side_effect = bot.LogMealHTTPError(429, "rate limit")
        statuses = []

        with self.assertRaises(bot.LogMealHTTPError):
            bot.recognize_image(self.image, "test-token", statuses.append)

        upload.assert_called_once()
        self.assertEqual(statuses, ["Отправил оригинал"])


class ConfirmAPITests(unittest.TestCase):
    @patch.object(bot, "post_json")
    def test_confirm_dish_calls_correct_endpoint(self, post):
        post.return_value = {"recognition_results": []}
        bot.confirm_dish("token", 123, [(1, 456), (2, 789)])
        post.assert_called_once()
        url = post.call_args.args[0]
        self.assertIn("/image/confirm/dish", url)
        body = json.loads(post.call_args.args[2])
        self.assertEqual(body["imageId"], 123)
        self.assertEqual(body["confirmedClass"], [456, 789])
        self.assertEqual(body["food_item_position"], [1, 2])

    @patch.object(bot, "post_json")
    def test_confirm_dish_empty_does_nothing(self, post):
        bot.confirm_dish("token", 123, [])
        post.assert_not_called()

    @patch.object(bot, "post_json")
    def test_confirm_quantity_calls_correct_endpoint(self, post):
        post.return_value = {"result": "OK"}
        bot.confirm_quantity("token", 123, {"1": 150, "2": 80})
        url = post.call_args.args[0]
        self.assertIn("/nutrition/confirm/quantity", url)
        body = json.loads(post.call_args.args[2])
        self.assertEqual(body["imageId"], 123)
        self.assertEqual(body["quantity"], {"1": 150, "2": 80})


class FormatResultTests(unittest.TestCase):
    def test_single_region_no_per_item_breakdown(self):
        nutrition = {
            "hasNutritionalInfo": True,
            "nutritional_info": {"calories": 280.2, "totalNutrients": {}},
            "serving_size": 220,
        }
        text = bot.format_result(123, nutrition, region_count=1)
        self.assertIn("Калории: 280.2 ккал", text)
        self.assertNotIn("⚠️", text)
        self.assertIn("Порция: 220 г", text)

    def test_multiple_regions_show_breakdown(self):
        nutrition = {
            "hasNutritionalInfo": True,
            "nutritional_info": {"calories": 400, "totalNutrients": {}},
            "serving_size": 440,
            "nutritional_info_per_item": [
                {"food_item_position": i, "serving_size": 220, "nutritional_info": {"calories": 200}}
                for i in (1, 2)
            ],
        }
        text = bot.format_result(123, nutrition, region_count=2)
        self.assertIn("⚠️", text)
        self.assertIn("#1: 220 г, 200.0 ккал", text)
        self.assertIn("#2: 220 г, 200.0 ккал", text)
        self.assertIn("Порция: 440 г", text)

    def test_no_nutritional_info(self):
        nutrition = {"hasNutritionalInfo": False}
        text = bot.format_result(123, nutrition)
        self.assertIn("не рассчитано", text)


class SessionTests(unittest.TestCase):
    def test_session_created_and_refreshed(self):
        old_sessions = bot.sessions.copy()
        bot.sessions.clear()
        try:
            s = bot._session(42)
            s2 = bot._session(42)
            self.assertAlmostEqual(s["ts"], s2["ts"], places=0)
            self.assertIn(42, bot.sessions)
        finally:
            bot.sessions.clear()
            bot.sessions.update(old_sessions)


class SpoonacularFormatTests(unittest.TestCase):
    def test_format_with_nutrition(self):
        recipe_info = {
            "title": "Pasta Carbonara",
            "nutrition": {
                "nutrients": [
                    {"name": "Calories", "amount": 574.0, "unit": "kcal"},
                    {"name": "Protein", "amount": 23.8, "unit": "g"},
                    {"name": "Fat", "amount": 30.2, "unit": "g"},
                    {"name": "Carbohydrates", "amount": 46.5, "unit": "g"},
                ]
            },
            "servings": 4,
        }
        text = bot.format_spoonacular_result(recipe_info)
        self.assertIn("Pasta Carbonara", text)
        self.assertIn("Калории: 574.0 ккал", text)
        self.assertIn("Белки: 23.8 г", text)
        self.assertIn("Жиры: 30.2 г", text)
        self.assertIn("Углеводы: 46.5 г", text)
        self.assertIn("Порции: 4", text)

    def test_format_missing_nutrients(self):
        recipe_info = {"title": "Unknown Dish", "nutrition": {"nutrients": []}}
        text = bot.format_spoonacular_result(recipe_info)
        self.assertIn("Unknown Dish", text)
        self.assertNotIn("Калории", text)


class SpoonacularAPITests(unittest.TestCase):
    @patch.object(bot, "spoonacular_analyze_image")
    def test_analyze_returns_recipes(self, analyze):
        analyze.return_value = {
            "recipes": [
                {"id": 640352, "title": "Caprese Salad", "confidence": 0.97},
                {"id": 716429, "title": "Pasta", "confidence": 0.02},
            ]
        }
        result = bot.spoonacular_analyze_image("test-key", b"fake-image")
        self.assertEqual(len(result["recipes"]), 2)
        self.assertEqual(result["recipes"][0]["title"], "Caprese Salad")

    @patch.object(bot, "spoonacular_get")
    def test_recipe_nutrition_calls_correct_endpoint(self, get):
        get.return_value = {"id": 640352, "title": "Caprese Salad", "nutrition": {"nutrients": []}}
        bot.spoonacular_recipe_nutrition("test-key", 640352)
        url = get.call_args.args[1]
        self.assertIn("/recipes/640352/information", url)
        params = get.call_args.args[2]
        self.assertEqual(params["includeNutrition"], "true")


class ProviderSwitchTests(unittest.TestCase):
    def test_build_provider_switch_keyboard_spoonacular(self):
        kb = bot._build_provider_switch_keyboard("spoonacular")
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "provider:logmeal")

    def test_build_provider_switch_keyboard_logmeal(self):
        kb = bot._build_provider_switch_keyboard("logmeal")
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "provider:spoonacular")

    def test_build_spoon_recipe_keyboard(self):
        recipes = [
            {"id": 1, "title": "Salad", "confidence": 0.9},
            {"id": 2, "title": "Pasta", "confidence": 0.1},
        ]
        kb = bot._build_spoon_recipe_keyboard(recipes)
        self.assertEqual(len(kb["inline_keyboard"]), 2)
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "spoon:1")
        self.assertIn("90%", kb["inline_keyboard"][0][0]["text"])


if __name__ == "__main__":
    unittest.main()
