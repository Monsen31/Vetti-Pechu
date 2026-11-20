# Vetti Pechu: Claude in your Discord group chat

*Vetti pechu* is Tamil for idle chatter.

A small Discord bot, powered by Claude, that joins the chat with you and your friends as Vetti Pechu: easy-going, up for banter, and genuinely helpful when someone asks a real question. Mention it (`@Vetti Pechu what do you reckon?`) or reply to one of its messages, and it reads the last 20 messages and answers like one of the group.

It runs for free on an Oracle Cloud "Always Free" server. The only cost is the Claude API, which with the Haiku model is a few pence a month for a group of five chatting normally.

**Files in this folder**

| File | What it is |
|---|---|
| `bot.py` | The bot itself |
| `.gitignore` | Stops `.env` and other private files ending up in your GitHub repo |
| `requirements.txt` | The Python packages it needs |
| `.env.example` | Template for your secret keys (copy to `.env`) |
| `vetti-pechu.service` | Keeps the bot running on the server, and restarts it if it crashes or the server reboots |

Total time: about 45 minutes, most of it waiting for Oracle signup.

---

## Step 1: Get a Claude API key (5 min)

A normal Claude.ai account (free or Pro) **can't** be used by a bot. You need an API account, which is separate and pay-as-you-go.

1. Go to **console.anthropic.com** and sign up.
2. Open **Settings > Billing** and add credit. £5 is plenty to start.
3. Open **Settings > Limits** and set a **monthly spend limit** (for example £5). The API stops when it's reached, so you can never get a surprise bill.
4. Open **API Keys > Create Key**. Name it `vetti-pechu` and copy the key (it starts with `sk-ant-`). Save it somewhere safe, you only see it once.

## Step 2: Create the Discord bot (10 min)

1. Go to **discord.com/developers/applications** and click **New Application**. Name it `Vetti Pechu` (this is the name your friends will see). You can also upload an avatar here.
2. In the left menu, open **Bot**.
   - Click **Reset Token**, then copy the token and keep it safe. This is your `DISCORD_TOKEN`.
   - Scroll down to **Privileged Gateway Intents** and switch on **Message Content Intent**. Click **Save Changes**. *Without this the bot can't read messages.*
3. In the left menu, open **OAuth2 > URL Generator**.
   - Under **Scopes**, tick `bot`.
   - Under **Bot Permissions**, tick **View Channels**, **Send Messages**, **Read Message History**.
   - Copy the URL at the bottom, open it in your browser, choose your server and click **Authorise**.

The bot now shows in your server's member list as offline. It comes online in Step 5.

> Don't have a server yet? In Discord, click the **+** on the left, choose **Create My Own**, then invite your friends.

## Step 3: Create a free Oracle Cloud server (15 to 20 min)

1. Sign up at **oracle.com/cloud/free**. Pick **UK South (London)** as your home region. Oracle asks for a card to verify you, but Always Free resources are never charged.
2. Once you're in the console, open the menu (top left) and go to **Compute > Instances > Create instance**.
3. Fill in:
   - **Name:** `vetti-pechu`
   - **Image:** click **Edit > Change image** and pick **Canonical Ubuntu 24.04**.
   - **Shape:** click **Change shape**. Choose **Ampere** and `VM.Standard.A1.Flex` with **1 OCPU and 6 GB memory**. (If that's unavailable, pick **Specialty and previous generation > VM.Standard.E2.1.Micro** instead. Both are marked "Always Free eligible". The bot needs very little.)
   - **SSH keys:** choose **Generate a key pair for me** and click **Save private key**. Keep that file (it ends in `.key`), it's how you log in.
4. Click **Create**. After a minute or two the status turns green (**Running**). Copy the **Public IP address**.

> **"Out of capacity" error?** It's common with the Ampere shape in busy regions. Either try again later, or use the `E2.1.Micro` shape, which is usually available.

No firewall changes are needed. The bot only makes outgoing connections.

## Step 4: Log in to the server and install the bot (10 min)

### Connect

On **Mac or Linux**, open Terminal. On **Windows 10/11**, open PowerShell. Then run (swap in your file name and IP):

```bash
chmod 600 ~/Downloads/ssh-key-2026-11-19.key      # Mac/Linux only
ssh -i ~/Downloads/ssh-key-2026-11-19.key ubuntu@YOUR.PUBLIC.IP
```

Type `yes` if it asks about the fingerprint. You're now on the server; everything below runs there.

### Install Python and copy the bot over

```bash
sudo apt update && sudo apt install -y python3-venv
mkdir -p ~/vetti-pechu
```

Now copy the files from this folder onto the server. Open a **second** terminal window on your own computer (not the server), go to the folder holding these files, and run:

```bash
scp -i ~/Downloads/ssh-key-2026-11-19.key bot.py requirements.txt .env.example vetti-pechu.service ubuntu@YOUR.PUBLIC.IP:~/vetti-pechu/
```

Back in the server window:

```bash
cd ~/vetti-pechu
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### Add your keys

```bash
cp .env.example .env
nano .env
```

Replace the two `REPLACE_ME` values with your Claude API key (Step 1) and Discord token (Step 2). Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**. Then lock the file so only you can read it:

```bash
chmod 600 .env
```

### Test it

```bash
venv/bin/python bot.py
```

You should see `Logged in as Vetti Pechu#1234`. In Discord, type `@Vetti Pechu hello!` in a channel. It should reply within a few seconds. Press **Ctrl+C** on the server to stop it.

## Step 5: Keep it running 24/7 (2 min)

This installs the bot as a service, so it starts on boot and restarts itself if it ever crashes.

```bash
sudo cp vetti-pechu.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vetti-pechu
```

Check it's running:

```bash
sudo systemctl status vetti-pechu
```

You should see **active (running)** in green. You can now close the terminal; the bot stays online.

### Handy commands

| To... | Run |
|---|---|
| See live logs | `journalctl -u vetti-pechu -f` (Ctrl+C to exit) |
| Restart after changing `.env` or `bot.py` | `sudo systemctl restart vetti-pechu` |
| Stop the bot | `sudo systemctl stop vetti-pechu` |
| Start it again | `sudo systemctl start vetti-pechu` |

---

## Customising

All settings live in `.env`. Restart the service after any change.

- **Personality:** the default persona is a laid-back, chatty friend. To change it, uncomment `SYSTEM_PROMPT=` and write your own, e.g. `SYSTEM_PROMPT=You are Vetti Pechu, the sarcastic but kind friend in our group chat. Keep replies to one or two sentences.`
- **Name:** defaults to Vetti Pechu. To change it, set `BOT_NAME=Jarvis` (also rename the app in the Discord Developer Portal so the display name matches).
- **Memory:** `HISTORY_LIMIT=40` lets it read further back. More history means slightly higher cost per reply.
- **@everyone and @here:** the bot also replies when someone pings `@everyone` or `@here`. Set `REPLY_TO_EVERYONE=off` to only reply when it is mentioned directly.
- **Web browsing:** on by default. The bot searches the web and reads links when someone asks about news, scores, prices or anything recent, and adds its sources under the reply. Set `WEB_BROWSING=off` to turn it off. `WEB_MAX_SEARCHES` (default 3) and `WEB_MAX_PAGE_READS` (default 2) cap how much it does per reply. If it never searches, check that web search is allowed for your organisation at **console.anthropic.com > Settings**.
- **Smarter model:** `CLAUDE_MODEL=claude-sonnet-5-5` gives noticeably better answers at roughly 20 times the cost of Haiku (still only pence a day for a small group).

## What it costs

Haiku 5.5 is priced at $0.10 per million input tokens and $0.50 per million output tokens. Each reply sends the last 20 messages (roughly 500 to 1,500 tokens) and gets a short answer back, so a reply costs a small fraction of a penny. Even 100 mentions a day comes to well under £1 a month. Web searches cost an extra $0.01 each (about 1p), plus the tokens for what it reads, and the bot only searches when a question needs it. Your spend limit from Step 1 is the safety net either way. You can watch usage at **console.anthropic.com > Usage**.

## Troubleshooting

| Problem | Fix |
|---|---|
| Bot is online but never replies | Message Content Intent is off (Step 2.2), or you didn't @mention it. Check the logs. |
| `discord.errors.PrivilegedIntentsRequired` | Same thing: switch on Message Content Intent and restart. |
| `LoginFailure: Improper token` | `DISCORD_TOKEN` in `.env` is wrong. Reset it in the portal and paste the new one. |
| Bot says "My API key isn't working" | `ANTHROPIC_API_KEY` in `.env` is wrong or was deleted. |
| Bot says "maybe out of API credit" | Add credit or raise your spend limit in the Anthropic console. |
| Bot can't post in a channel | Give its role **Send Messages** and **Read Message History** in that channel's permissions. |
| `ssh: Permission denied (publickey)` | Use the `.key` file you downloaded when creating the instance, and the user `ubuntu`. |

## Keeping it safe

- Never share or post `.env`, your Claude API key or your Discord token. If one leaks, delete/reset it straight away in the Anthropic console or Discord portal and put the new one in `.env`.
- Anyone in your server can talk to the bot, and every reply costs a little. Keep the server to friends and keep the spend limit on.
