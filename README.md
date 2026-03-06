# 🎰 Casino Bot — Деплой на Railway

## 📦 Зависимости
```bash
pip install -r requirements.txt
```

---

## 🚀 Деплой на Railway (пошагово)

### Шаг 1 — GitHub репозиторий
1. Зайди на [github.com](https://github.com) → **New repository**
2. Назови `casino-bot` → **Create repository**
3. Загрузи все файлы через **Add file → Upload files**:
   `bot.py`, `database.py`, `config.py`, `requirements.txt`, `Procfile`, `railway.json`

### Шаг 2 — Регистрация на Railway
1. Зайди на [railway.app](https://railway.app)
2. **Login with GitHub**

### Шаг 3 — Создай проект
1. **New Project → Deploy from GitHub repo**
2. Выбери репозиторий `casino-bot`

### Шаг 4 — Переменные окружения
> ⚠️ Не храни токен в config.py на GitHub!

Во вкладке **Variables** добавь:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | твой токен от BotFather |
| `ADMIN_IDS` | твой Telegram ID |

### Шаг 5 — Обнови config.py
Замени первые 2 строки на:
```python
import os
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "123456789").split(",")]
```

### Шаг 6 — Проверь логи
Вкладка **Deployments** → должно появиться:
```
✅ База данных инициализирована
🌐 Веб-панель запущена
🤖 Бот запущен!
```

### Шаг 7 — Веб-панель
**Settings → Networking → Generate Domain**
Открой: `https://твой-домен.up.railway.app/admin?pass=casino_admin_2024`

---

## 🔔 Inline-режим
1. @BotFather → `/setinline` → выбери бота → введи подсказку (`Поиск игры...`)
2. В любом чате пиши `@твой_бот` + пробел

---

## 🎮 Ключевые слова
`слоты 100` · `кости 100` · `рулетка 100` · `карты 100` · `краш 100`
`баланс` · `профиль` · `бонус` · `задания` · `топ` · `магазин`
