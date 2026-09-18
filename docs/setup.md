# Подключение: GitHub, Actions, Pages

Всё бесплатно. Займёт 10–15 минут.

## МОЁ ДЕЙСТВИЕ №1 — создать репозиторий

1. Откройте https://github.com/new.
2. Repository name: `moex-paper-bot`.
3. Видимость: **Public**. Почему: сайт-дашборд на GitHub Pages бесплатен только для публичных репозиториев, а секретов в проекте нет. Минус — код и виртуальные сделки видны всем. Если нужен Private, дашборд придётся открывать иначе (скажите — переделаю).
4. **Не** ставьте галочки README, .gitignore, license — репозиторий должен быть пустым.
5. Create repository.

## МОЁ ДЕЙСТВИЕ №2 — токен, чтобы я мог отправить код

1. GitHub → аватар → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
2. Token name: `claude-moex-paper-bot`. Expiration: **7 days** (после загрузки кода он не нужен).
3. Repository access: **Only select repositories** → `moex-paper-bot`.
4. Repository permissions:
   - **Contents: Read and write**
   - **Workflows: Read and write** — без этого GitHub не примет файлы автоматизации из `.github/workflows`.
5. Generate token → скопируйте и отправьте в чат **отдельным сообщением** вместе с адресом репозитория. После загрузки токен можно сразу удалить (Settings → Fine-grained tokens → Delete).

## МОЁ ДЕЙСТВИЕ №3 — разрешить боту записывать результаты

Репозиторий → **Settings** → **Actions** → **General** → раздел *Workflow permissions* → **Read and write permissions** → Save.

## МОЁ ДЕЙСТВИЕ №4 — первый запуск

1. Вкладка **Actions** → слева «Обновить историю котировок» → **Run workflow** → Run. Бот докачает историю Мосбиржи с 2021 года по сегодня (до ~30 минут). Это же проверит главное: пускает ли Мосбиржа серверы GitHub.
2. Когда он станет зелёным: «Бумажная торговля» → **Run workflow**. Появится ветка `live`.
3. Если что-то красное — пришлите скриншот лога или текст ошибки.

## МОЁ ДЕЙСТВИЕ №5 — включить сайт

Settings → **Pages** → Source: *Deploy from a branch* → Branch: **live**, папка **/ (root)** → Save.
Через 1–2 минуты сайт будет по адресу `https://<ваш-логин>.github.io/moex-paper-bot/`.

Дальше бот работает сам: каждые 30 минут в будни с 9:00 до 24:00 МСК.
