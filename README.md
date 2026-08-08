# Rift Roll — мінімальна версія для Render + Aiven

У проєкті немає React, Node.js, D1, Alembic або окремої папки зі статикою.
Весь бекенд, моделі БД та API містяться в `app.py`, а весь інтерфейс, CSS і
JavaScript — у `templates/index.html`.

## Що вже працює

- реєстрація, логін, захищена cookie-сесія та вихід;
- серверний RNG, luck, cooldown і незалежні шанси мутацій;
- інвентар із дублікатами та пасивним доходом;
- зілля luck/speed із таймерами;
- часові й ручні івенти з event-exclusive контентом;
- Index і глобальний Roll of the Hour;
- публічні профілі для авторизованих гравців;
- 4 сторінки вітрини по 8 карток;
- Admin Studio: CRUD карток, мутацій, івентів і зілля;
- автоматичне створення всіх таблиць і стартових даних.

## 1. Підготуй Aiven PostgreSQL

1. Створи PostgreSQL service в Aiven.
2. Відкрий сторінку `Overview` сервісу.
3. Скопіюй повний `Service URI` для PostgreSQL. Він має виглядати приблизно так:

   `postgresql://avnadmin:пароль@host.aivencloud.com:порт/defaultdb?sslmode=require`

Не публікуй цей URI в GitHub — він містить пароль від бази.

## 2. Завантаж проєкт у GitHub

1. Розпакуй ZIP.
2. Створи порожній GitHub-репозиторій.
3. Завантаж у корінь репозиторію саме вміст папки `rift-roll-render-aiven`:
   `app.py`, `templates`, `requirements.txt`, `render.yaml` та інші файли поруч.

`app.py` має лежати в корені, а HTML — за шляхом
`templates/index.html`.

## 3. Розгорни на Render

Найпростіший спосіб:

1. У Render натисни `New` → `Blueprint`.
2. Підключи GitHub-репозиторій.
3. Render прочитає `render.yaml` і попросить значення секретів.
4. Вкажи:

   - `DATABASE_URL` — повний Aiven Service URI;
   - `ADMIN_EMAIL` — email, з яким ти зареєструєш власний акаунт.

`SECRET_KEY` Render згенерує сам. Після запуску відкрий видану Render-адресу та
зареєструйся з email, указаним у `ADMIN_EMAIL`. Саме цей акаунт матиме доступ до
Admin Studio.

На першому запуску застосунок сам створить таблиці та стартовий пул. Окремо
запускати SQL або міграції не потрібно.

## Локальна перевірка

Без `DATABASE_URL` застосунок автоматично використовує локальний SQLite-файл —
це лише для перевірки на комп’ютері:

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

На Linux/macOS команда активації: `source .venv/bin/activate`.

Відкрий `http://127.0.0.1:8000`. Перший зареєстрований локальний акаунт стає
адміністратором, якщо `ADMIN_EMAIL` не задано.

## Важливо

- У продакшені обов’язково використовуй `ADMIN_EMAIL` і сильний `SECRET_KEY`.
- Render не бере код прямо із ZIP: його потрібно розпакувати й покласти в GitHub.
- Для наступних змін структури вже живої БД краще додати Alembic. У цій першій
  мінімальній версії його навмисно немає, щоб деплой складався з кількох файлів.
