<div align="center">

# ⚡ InstaAutoReply (CommentDM)

**A lightweight, production-ready Instagram Comment-to-DM automation engine.**  
Built to run reliably on **PythonAnywhere's free tier**, VPS, or local development with **zero external dependencies** (no Redis, no Celery, no Docker required).

---

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg?style=flat-square&logo=python)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-black.svg?style=flat-square&logo=flask)](https://flask.palletsprojects.com/)
[![Meta Graph API](https://img.shields.io/badge/Meta_Graph_API-v21.0-0866FF.svg?style=flat-square&logo=meta)](https://developers.facebook.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?style=flat-square)](LICENSE)

</div>

---

## 📋 Table of Contents

- [Features](#-features)
- [Architecture & Tech Stack](#-architecture--tech-stack)
- [Quick Start: PythonAnywhere Deployment](#-quick-start-pythonanywhere-deployment-free-tier)
- [Meta / Facebook Developer Setup](#-meta--facebook-developer-setup)
  - [1. Create Meta Developer App](#1-create-a-meta-developer-app)
  - [2. Generate Access Token (Dual-Compatible)](#2-generate-an-access-token)
  - [3. Configure Instagram Webhook](#3-configure-instagram-webhooks)
  - [4. Add Compliance URLs](#4-add-meta-compliance-urls)
- [Local Development & Testing](#-local-development--testing)
- [Admin Dashboard Walkthrough](#-admin-dashboard-walkthrough)
- [Troubleshooting & Common Issues](#-troubleshooting--common-issues)
- [Project Structure](#-project-structure)
- [Compliance & Privacy](#-compliance--privacy)

---

## ✨ Features

- 🎯 **Follower-Aware Messaging**: Deliver **Message A** to active followers and **Message B** to non-followers (e.g. asking them to follow to unlock a bonus resource).
- 💬 **Public Comment Auto-Reply**: Optionally post a randomized or custom public reply to the user's comment to boost post engagement algorithmically.
- 🔄 **Dual Meta API Compatibility**:
  - Supports **Instagram User Tokens** (`IG...` / `IGAA...`) routed automatically to `graph.instagram.com`.
  - Supports **Facebook Page Access Tokens** (`EAA...`) routed automatically to `graph.facebook.com`.
- ⚡ **Granular Automation Rules**:
  - Filter triggers by specific keywords (comma-separated) or reply to all incoming comments.
  - Target specific Instagram posts/reels or apply automations globally to all content.
- 📊 **Built-In Lightweight Dashboard**:
  - Live activity feed & conversion analytics (delivered, follower vs non-follower, duplicates, failures).
  - 4-step interactive wizard for creating and editing automations.
  - Zero frontend build step (Tailwind CSS CDN + vanilla JS).
- 🛡️ **Meta Review & Platform Compliant**:
  - Built-in `GET /privacy` (Privacy Policy) page.
  - Built-in `GET /data-deletion` (Step-by-step user instructions & confirmation tracking).
  - Built-in `POST /data-deletion` (Meta Signed Request callback handler).
- 🔒 **Secure & Idempotent**:
  - SQLite database with WAL mode ensures fast reads/writes with automatic deduplication so users never receive duplicate DMs.
  - Protected single-admin auth with password hashing (`werkzeug.security`).

---

## 🏗 Architecture & Tech Stack

```
                               ┌────────────────────────┐
                               │  Instagram User Post   │
                               └───────────┬────────────┘
                                           │ (User comments keyword)
                                           ▼
┌──────────────────┐           ┌────────────────────────┐
│  Meta Graph API  │ ◄──────── │   Meta Webhook Server  │
└────────┬─────────┘           └───────────┬────────────┘
         │                                 │ (POST /webhook)
         │                                 ▼
         │                     ┌────────────────────────┐
         │ (Send DM & Reply)   │  InstaAutoReply Engine │
         └──────────────────── │  (Flask + SQLite WAL)  │
                               └────────────────────────┘
```

- **Backend**: Python 3.9+, Flask
- **Database**: SQLite3 (Single-file persistent storage, WAL enabled)
- **HTTP Client**: `requests` with exponential backoff retries & connection pooling
- **Frontend**: Embedded Single Page Application (Tailwind CSS CDN, Inter / JetBrains Mono)
- **Hosting Target**: PythonAnywhere (Free / Hacker tier) or any Linux VPS

---

## 🚀 Quick Start: PythonAnywhere Deployment (Free Tier)

PythonAnywhere's free tier allows outbound HTTPS requests to allowlisted domains. Both `graph.instagram.com` and `graph.facebook.com` are allowlisted by default.

### Step 1: Create PythonAnywhere Account
1. Sign up for a free account at [pythonanywhere.com](https://www.pythonanywhere.com/).

### Step 2: Open a Bash Console & Clone Repository
1. Navigate to the **Consoles** tab and click **Bash**.
2. Clone this repository into your home directory:
   ```bash
   git clone https://github.com/talha-khallid/InstaAutoReply.git
   cd InstaAutoReply
   ```
3. Install required Python packages:
   ```bash
   pip install --user flask requests werkzeug
   ```

### Step 3: Configure Web App
1. Go to the **Web** tab in PythonAnywhere.
2. Click **Add a new web app**.
3. Choose **Manual configuration** (do **not** choose Django or automatic Flask), and select **Python 3.10** (or 3.11).
4. In the **Virtualenv** section (optional), or use system Python.
5. In the **Code** section:
   - **Source code**: `/home/<your-username>/InstaAutoReply`
   - **Working directory**: `/home/<your-username>/InstaAutoReply`
6. Click on the **WSGI configuration file** link (e.g. `/var/www/<your-username>_pythonanywhere_com_wsgi.py`).
7. Replace its entire contents with:
   ```python
   import sys
   import os

   path = '/home/<your-username>/InstaAutoReply'
   if path not in sys.path:
       sys.path.append(path)

   from app import app as application
   ```
   *(Replace `<your-username>` with your actual PythonAnywhere username)*
8. Save the file and go back to the **Web** tab.
9. Click the green **Reload <your-username>.pythonanywhere.com** button.

### Step 4: Complete Initial Admin Setup
1. Visit `https://<your-username>.pythonanywhere.com/setup` in your browser.
2. Create your admin username and password (at least 8 characters).
3. Log in to your CommentDM dashboard.

---

## 🛠 Meta / Facebook Developer Setup

To connect Instagram automation, you need an authorized Meta Developer App.

### 1. Create a Meta Developer App
1. Go to the [Meta for Developers Portal](https://developers.facebook.com/) and log in.
2. Click **My Apps** &rarr; **Create App**.
3. Select **Other** &rarr; **Next** &rarr; Select **Business** or **Consumer** &rarr; **Next**.
4. Enter an **App Display Name** (e.g. `CommentDM Automation`) and your contact email. Click **Create app**.

---

### 2. Generate an Access Token

InstaAutoReply supports **both** token types automatically:

#### Option A: Instagram User Token (`IG...`) — Recommended for Creators
1. In your Meta App Dashboard, add the **Instagram** / **Instagram API with Instagram Login** product.
2. Add your Instagram account as a Test User or connect via Instagram Login.
3. Generate a User Access Token. It will start with `IG...` or `IGAA...`.
4. Required permissions:
   - `instagram_business_basic`
   - `instagram_business_manage_messages`
   - `instagram_business_manage_comments`

#### Option B: Facebook Page Token (`EAA...`) — For Business Pages
1. In your Meta App Dashboard, add **Instagram Graph API** and **Facebook Login for Business**.
2. Link your Instagram Professional account (Business or Creator) to a Facebook Page in Instagram Settings.
3. In Graph API Explorer, select your App and generate a User Token with:
   - `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_manage_comments`, `instagram_manage_messages`
4. Exchange it for a Long-Lived Page Access Token (starts with `EAAB...` or `EAA...`).

---

### 3. Configure Instagram Webhooks

1. In your Meta App dashboard, navigate to **Webhooks** (or **Instagram** &rarr; **Webhooks**).
2. Set the **Callback URL** to:
   ```
   https://<your-username>.pythonanywhere.com/webhook
   ```
3. Set the **Verify Token** to the token shown under **Settings** &rarr; **Webhook** in your CommentDM dashboard.
4. Click **Verify and Save**.
5. Subscribe to the **`comments`** field for your connected Instagram / Page account.

---

### 4. Add Meta Compliance URLs

Meta requires valid compliance URLs in your **App settings &rarr; Basic**:

| Meta Field | URL to Provide | Method |
|---|---|---|
| **Privacy Policy URL** | `https://<your-username>.pythonanywhere.com/privacy` | `GET` |
| **User Data Deletion URL** | `https://<your-username>.pythonanywhere.com/data-deletion` | `GET` / `POST` |
| **Data Deletion Callback** | `https://<your-username>.pythonanywhere.com/data-deletion` | `POST` (Callback) |

*Both URLs are accessible out of the box and can be copied in 1 click from the dashboard Settings tab.*

---

## 💻 Local Development & Testing

### Running Locally
```bash
# 1. Clone repo
git clone https://github.com/talha-khallid/InstaAutoReply.git
cd InstaAutoReply

# 2. Install dependencies
pip install flask requests werkzeug

# 3. Start local server
python app.py
```
App will start on `http://localhost:5000`.

### Testing Webhooks Locally with ngrok / Cloudflare Tunnel
Since Meta webhooks require a publicly accessible HTTPS endpoint:
```bash
ngrok http 5000
```
Copy the forwarding HTTPS address (e.g. `https://xyz123.ngrok-free.app`) and use:
- **Callback URL**: `https://xyz123.ngrok-free.app/webhook`
- **Verify Token**: Found in CommentDM Settings

---

## 🖥 Admin Dashboard Walkthrough

### 1. Activity Log & Metrics
Track real-time stats including:
- **Total Comments Received**
- **DMs Delivered**
- **Follower vs. Non-Follower DMs**
- **Duplicate Comments Ignored** (Prevents spam)
- **Detailed activity log** with status badges (`sent`, `failed`, `skipped`)

### 2. Creating an Automation (4-Step Wizard)
1. **Name & Media**: Name your automation and choose whether it applies to **All Posts** or a **Specific Post/Reel**.
2. **Trigger Rules**:
   - *Reply to every comment* OR
   - *Keyword match* (e.g. `guide, send, link, ebook`).
3. **Follower-Aware Responses**:
   - **Message A (Followers)**: Direct link / full resource delivery.
   - **Message B (Non-Followers)**: Polite prompt + bonus link.
4. **Public Reply**: Optional automated public reply to comment (e.g. *"Check your DMs! 📩"*).

---

## ❓ Troubleshooting & Common Issues

<details>
<summary><strong>1. "Invalid OAuth access token - Cannot parse the access token"</strong></summary>

This error happens when an `IG...` token is sent to `graph.facebook.com` instead of `graph.instagram.com`.  
**Solution**: This codebase includes dynamic token routing (`get_base_url`). It automatically detects whether your token is an Instagram native token (`IG...`) or a Facebook Page token (`EAA...`) and sends requests to the correct endpoint.
</details>

<details>
<summary><strong>2. Webhook verification fails ("Forbidden 403")</strong></summary>

- Verify that the **Verify Token** pasted into Meta exactly matches the value in your CommentDM dashboard Settings tab.
- Ensure your PythonAnywhere web app is reloaded and active.
</details>

<details>
<summary><strong>3. Comments are received but no DMs are sent</strong></summary>

- Check the **Activity Log** tab in your CommentDM dashboard.
- If error is `"Instagram account not connected"`, go to Settings and click **Save & connect** with your token.
- Ensure the user commenting does not have their DMs restricted to friends only.
- In Meta App Development mode, direct messages can only be delivered to accounts listed under **Roles &rarr; Test Users / Developers**. Switch app to **Live** mode for public users.
</details>

<details>
<summary><strong>4. Follower check always returns False</strong></summary>

- Meta's `is_user_follow_business` field is only provided for **Page tokens (`EAA...`)**.
- When using Instagram-native creator tokens (`IG...`), the system defaults to Message B (or a unified flow) gracefully without breaking.
</details>

---

## 📁 Project Structure

```
InstaAutoReply/
│
├── app.py                  # Single-file Flask app (routes, DB, Meta API, embedded UI)
├── .gitignore              # Ignores SQLite databases, pycache, venv, and local configs
├── README.md               # Complete setup, deployment, and Meta API guide
└── automation.db           # Auto-created SQLite database (created on first run)
```

---

## 🔒 Compliance & Privacy

- **Data Minimization**: Stores only interaction timestamps, comment IDs, and user IDs strictly for webhook deduplication.
- **No Third-Party Brokers**: Data is stored locally in your SQLite instance and is never transmitted to third parties.
- **Meta Platform Policy**: Fully supports user data deletion callbacks (`POST /data-deletion`) and user removal instructions (`GET /data-deletion`).

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
