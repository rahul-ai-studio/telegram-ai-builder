# 🤖 Telegram AI Builder Bot

A Telegram bot that generates **complete, runnable project scaffolds** using the Claude AI API. Send a single `/build` command and receive a fully structured codebase saved to your machine. Also supports natural language chat, image analysis, and PDF summarization — all from within Telegram.

---

## ✨ Features

| Feature | Description |
|---|---|
| `/build` | Generate a complete project (files, README, `.gitignore`) from a one-line description |
| **AI Chat** | Every plain text message gets a context-aware Claude reply (last 10 messages remembered) |
| **Image Analysis** | Send any photo or image file — Claude describes it, extracts text, explains details |
| **PDF Summarization** | Send a PDF — the bot extracts text (or renders pages for scanned PDFs) and summarizes it |
| **Persistent Memory** | SQLite-backed conversation history and build task log that survives restarts |
| **Smart Routing** | Automatically routes tasks to Haiku (fast/cheap), Sonnet (standard), or Opus (complex) |

---

## 🏗 Architecture

```
telegram-ai-builder/
├── bot.py          # Telegram handlers (commands, messages, photos, documents)
├── builder.py      # Project generation via Claude + QA review
├── router.py       # Model selection (Haiku / Sonnet / Opus) + cost estimation
├── memory.py       # SQLite persistence (conversations, tasks, snippets)
├── media.py        # Image analysis + PDF summarization (vision + text extraction)
├── prompts.py      # System prompt constants
├── requirements.txt
├── .env.example
└── data/           # Auto-created — holds memory.db (gitignored)
```

### Model Routing Logic

| Task Type | Model | When |
|---|---|---|
| Simple Q&A, summaries | **Haiku 4.5** | Keywords: *what is, summarize, translate, explain briefly…* |
| Standard chat, builds | **Sonnet 4.5** | Default for all other tasks |
| Complex / architectural | **Opus 4.5** | Keywords: *architecture, system design, enterprise, security audit…* |

> Build tasks (`/build`) always use **at least Sonnet**, regardless of routing.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.9+
- A Telegram Bot token → get one from [@BotFather](https://t.me/BotFather)
- An Anthropic API key → get one at [console.anthropic.com](https://console.anthropic.com)

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/yourusername/telegram-ai-builder.git
cd telegram-ai-builder

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
```

Open `.env` and fill in your values:

```dotenv
TELEGRAM_BOT_TOKEN=7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OUTPUT_DIR=~/ai-builder-output
TIMEZONE=UTC
```

### Run

```bash
python3 bot.py
```

---

## 🔑 Getting Your API Keys

### Telegram Bot Token + Chat ID

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)**
2. Send `/newbot` and follow the prompts to name your bot
3. Copy the token BotFather gives you into `TELEGRAM_BOT_TOKEN`
4. Start your bot, send it `/start`, and it will reply with **"Your Chat ID: `123456789`"** — paste that into `TELEGRAM_CHAT_ID`

### Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com) and sign in
2. Navigate to **API Keys** in account settings
3. Click **Create Key**, copy it into `ANTHROPIC_API_KEY`

---

## 📖 Commands

| Command | Usage | Description |
|---|---|---|
| `/start` | `/start` | Welcome message and your Chat ID |
| `/help` | `/help` | Same as `/start` |
| `/build` | `/build a REST API for a todo app` | Generate a complete project |
| `/status` | `/status` | Show your 5 most recent builds |
| `/agents` | `/agents` | List internal modules and their roles |
| `/clear` | `/clear` | Reset conversation history for this chat |
| `/reset` | `/reset` | Alias for `/clear` |

### Non-command Interactions

| Input | Behavior |
|---|---|
| Any text | AI chat reply using the last 10 messages as context |
| 📷 Photo | Vision analysis — description, text extraction, notable details |
| 📄 PDF file | Smart summarization (text extraction → vision fallback for scanned PDFs) |
| 🖼 Image file | Vision analysis, same as photo |

---

## 📁 Build Output

Generated projects are saved to `~/ai-builder-output/<project-name>/` by default. Each build produces:

- All source code files
- `README.md` with setup instructions
- `.gitignore`
- A QA review from Claude Haiku highlighting potential issues

Override the output directory by setting `OUTPUT_DIR` in your `.env`.

---

## 📦 PDF Handling

The bot handles PDFs intelligently:

1. **Text-based PDFs** — text is extracted directly via PyMuPDF and sent to Claude (fast, handles any number of pages)
2. **Scanned PDFs** — pages are rendered as JPEG images and sent to Claude's vision API (up to 6 pages)

---

## 💰 Cost Estimation

Every API call logs an estimated cost using Anthropic's published rates:

| Model | Input | Output |
|---|---|---|
| Haiku 4.5 | $1 / MTok | $5 / MTok |
| Sonnet 4.5 | $3 / MTok | $15 / MTok |
| Opus 4.5 | $5 / MTok | $25 / MTok |

Costs are shown as a footer on every chat reply.

---

## 🛠 Tech Stack

- **[python-telegram-bot v21](https://github.com/python-telegram-bot/python-telegram-bot)** — async Telegram bot framework
- **[Anthropic Claude API](https://docs.anthropic.com)** — AI chat, code generation, vision, and analysis
- **[PyMuPDF (fitz)](https://pymupdf.readthedocs.io)** — PDF text extraction and page rendering
- **SQLite** — zero-config persistent storage (via Python stdlib `sqlite3`)
- **python-dotenv** — environment variable management

---

## 🔒 Security Notes

- Never commit your `.env` file — it is listed in `.gitignore`
- The `data/` directory (SQLite database) is also gitignored
- API keys are read exclusively from environment variables — nothing is hardcoded

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
