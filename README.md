# InstaAutoReply (CommentDM)

A lightweight Instagram Comment-to-DM automation engine designed to run on PythonAnywhere (free tier), any Linux VPS, or local environments without external dependencies like Redis, Celery, or Docker.

---

## Table of Contents

- [Overview & Features](#overview--features)
- [Architecture & Tech Stack](#architecture--tech-stack)
- [PythonAnywhere Deployment (Free Tier)](#pythonanywhere-deployment-free-tier)
- [Meta / Facebook Developer Setup](#meta--facebook-developer-setup)
  - [1. Create Meta Developer App](#1-create-meta-developer-app)
  - [2. Generate Access Token](#2-generate-access-token)
  - [3. Configure Instagram Webhook](#3-configure-instagram-webhook)
  - [4. Set Compliance URLs](#4-set-compliance-urls)
- [Local Development & Testing](#local-development--testing)
- [Dashboard & Automation Rules](#dashboard--automation-rules)
- [Troubleshooting & Common Issues](#troubleshooting--common-issues)
- [Project Structure](#project-structure)
- [Privacy & Compliance](#privacy--compliance)
- [License](#license)

---

## Overview & Features

- **Follower-Aware Direct Messaging**: Sends separate, configurable messages to active followers (Message A) and non-followers (Message B).
- **Public Comment Replies**: Automatically posts public reply comments to boost post interaction and confirm DM dispatch.
- **Dual Meta Graph API Compatibility**:
  - Automatically routes Instagram User Tokens (`IG...` / `IGAA...`) to `graph.instagram.com`.
  - Automatically routes Facebook Page Access Tokens (`EAA...`) to `graph.facebook.com`.
- **Targeting & Trigger Controls**:
  - Target specific Instagram posts/reels or apply automations globally across all media.
  - Trigger by comma-separated keyword matching or respond to every incoming comment.
- **Embedded Admin Dashboard**:
  - Single-page dashboard with real-time delivery metrics, activity logs, and a 4-step automation wizard.
  - Zero build step required (vanilla JS and Tailwind CSS CDN).
- **Meta Policy & Review Ready**:
  - Built-in `GET /privacy` (Privacy Policy) and `GET /terms` (Terms of Service) pages.
  - Built-in `GET /data-deletion` (user instructions) and `POST /data-deletion` (Meta Signed Request callback).
- **Deduplication & Reliability**:
  - SQLite backend with WAL mode to ensure idempotency and prevent duplicate DMs on retried webhooks.
  - Built-in exponential backoff for resilient API requests.

---

## Architecture & Tech Stack

```
                              +------------------------+
                              |  Instagram User Post   |
                              +-----------+------------+
                                          | (Comment posted)
                                          v
+------------------+          +------------------------+
|  Meta Graph API  | <------- |   Meta Webhook Server  |
+--------+---------+          +-----------+------------+
         |                                | (POST /webhook)
         |                                v
         |                    +------------------------+
         | (Send DM & Reply)  |  InstaAutoReply Engine |
         +------------------- |  (Flask + SQLite WAL)  |
                              +------------------------+
```

- **Backend**: Python 3.9+, Flask
- **Database**: SQLite3 (Single-file storage, WAL enabled)
- **HTTP Client**: `requests` with exponential backoff retry logic
- **Frontend**: Embedded dashboard (Tailwind CSS, Inter / JetBrains Mono)
- **Hosting**: PythonAnywhere (Free tier compatible) or any Linux VPS

---

## PythonAnywhere Deployment (Free Tier)

PythonAnywhere free accounts allow outbound HTTPS requests to allowlisted domains. Both `graph.instagram.com` and `graph.facebook.com` are on the allowlist.

### 1. Create a PythonAnywhere Account
Sign up for a free account at [pythonanywhere.com](https://www.pythonanywhere.com/).

### 2. Clone Repository & Install Dependencies
1. Open a **Bash console** from the **Consoles** tab.
2. Clone the repository into your home directory:
   ```bash
   git clone https://github.com/talha-khallid/InstaAutoReply.git
   cd InstaAutoReply
   ```
3. Install required packages:
   ```bash
   pip install --user flask requests werkzeug
   ```

### 3. Configure the Web App
1. Go to the **Web** tab.
2. Click **Add a new web app**.
3. Choose **Manual configuration** and select **Python 3.10** (or newer).
4. Under the **Code** section:
   - **Source code**: `/home/<your-username>/InstaAutoReply`
   - **Working directory**: `/home/<your-username>/InstaAutoReply`
5. Click on the **WSGI configuration file** link and replace its contents with:
   ```python
   import sys
   import os

   path = '/home/<your-username>/InstaAutoReply'
   if path not in sys.path:
       sys.path.append(path)

   from app import app as application
   ```
   *(Replace `<your-username>` with your PythonAnywhere username)*
6. Save the file, return to the **Web** tab, and click **Reload <your-username>.pythonanywhere.com**.

### 4. Admin Setup
1. Open `https://<your-username>.pythonanywhere.com/setup` in your browser.
2. Create an admin username and password (minimum 8 characters).
3. Log in to access the dashboard.

---

## Meta / Facebook Developer Setup

An authorized Meta Developer App is required to receive webhooks and send messages.

### 1. Create Meta Developer App
1. Go to the [Meta for Developers Portal](https://developers.facebook.com/) and log in.
2. Click **My Apps** -> **Create App**.
3. Select **Other** -> **Business** (or Consumer with Instagram Graph API) -> **Next**.
4. Enter an App Name and Contact Email, then create the app.

---

### 2. Generate Access Token

The application supports both token types automatically:

#### Option A: Instagram User Token (`IG...`)
1. In your Meta App Dashboard, add the **Instagram** product (Instagram API with Instagram Login).
2. Connect your Instagram Business or Creator account.
3. Generate a User Token with the following permissions:
   - `instagram_business_basic`
   - `instagram_business_manage_messages`
   - `instagram_business_manage_comments`

#### Option B: Facebook Page Token (`EAA...`)
1. In your Meta App Dashboard, add **Instagram Graph API** and **Facebook Login for Business**.
2. Link your Instagram Professional account to a Facebook Page.
3. Generate a Long-Lived Page Access Token with:
   - `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_manage_comments`, `instagram_manage_messages`

---

### 3. Configure Instagram Webhook

1. In your Meta App dashboard, navigate to **Webhooks** -> **Instagram**.
2. Set the **Callback URL** to:
   ```
   https://<your-username>.pythonanywhere.com/webhook
   ```
3. Set the **Verify Token** to the value displayed in your CommentDM dashboard under **Settings** -> **Webhook**.
4. Click **Verify and Save**.
5. Subscribe to the **`comments`** field for your connected Instagram account.

---

### 4. Set Compliance URLs

Meta requires compliance URLs in **App settings -> Basic**:

| Meta Field | Endpoint URL | Method |
|---|---|---|
| Privacy Policy URL | `https://<your-username>.pythonanywhere.com/privacy` | `GET` |
| Terms of Service URL | `https://<your-username>.pythonanywhere.com/terms` | `GET` |
| User Data Deletion URL | `https://<your-username>.pythonanywhere.com/data-deletion` | `GET` / `POST` |
| Data Deletion Callback | `https://<your-username>.pythonanywhere.com/data-deletion` | `POST` |

*(These URLs can be copied directly from the Settings tab in the dashboard.)*

---

## Local Development & Testing

### Running Locally
```bash
# Clone the repository
git clone https://github.com/talha-khallid/InstaAutoReply.git
cd InstaAutoReply

# Install dependencies
pip install flask requests werkzeug

# Run application
python app.py
```
The server will start at `http://localhost:5000`.

### Local Webhook Testing via Tunnel
To expose your local server to Meta webhooks:
```bash
ngrok http 5000
```
Use the generated HTTPS URL as your callback:
- **Callback URL**: `https://<subdomain>.ngrok-free.app/webhook`
- **Verify Token**: Found in your dashboard Settings tab

---

## Dashboard & Automation Rules

### Activity Monitoring
The dashboard tracks:
- Total comments received vs. duplicates ignored.
- DMs delivered, broken down by followers vs. non-followers.
- Dispatch failure logs and error details.

### Automation Creation Workflow
1. **Name & Media**: Specify an automation name and select either a specific post/reel or all posts.
2. **Trigger Condition**: Choose between keyword matching (comma-separated list) or replying to every comment.
3. **Message Configuration**:
   - **Message A**: Sent to users who follow the account.
   - **Message B**: Sent to users who do not follow the account.
4. **Public Reply**: Optional automated comment response on the post.

---

## Troubleshooting & Common Issues

#### 1. "Invalid OAuth access token - Cannot parse the access token"
- **Cause**: An `IG...` token was sent to `graph.facebook.com` instead of `graph.instagram.com`.
- **Solution**: The application handles this automatically via `get_base_url()`, dynamically routing `IG...` tokens to Instagram's endpoint and `EAA...` tokens to Facebook's endpoint.

#### 2. Webhook verification fails ("Forbidden 403")
- Ensure the Verify Token entered in the Meta dashboard matches the value in CommentDM Settings.
- Verify that the web app is running and reloaded.

#### 3. Comments received but no DMs sent
- Check the Activity Log tab in the dashboard for error codes.
- Ensure the Instagram account is connected via **Settings -> Save & connect**.
- In Meta Development Mode, DMs can only be delivered to accounts configured as Testers or Developers in the Meta App. Switch the app to Live Mode for public users.

#### 4. Follower status returns false
- The `is_user_follow_business` API field is only supported for Facebook Page tokens (`EAA...`).
- When using Instagram User tokens (`IG...`), the system defaults to Message B without throwing errors.

---

## Project Structure

```
InstaAutoReply/
├── app.py              # Main application (routes, Meta API helpers, embedded dashboard)
├── .gitignore          # Excludes SQLite databases, caches, and environments
└── README.md           # Setup, deployment, and configuration documentation
```

---

## Privacy & Compliance

- **Data Minimization**: Stores only interaction timestamps, comment IDs, and user IDs required for message deduplication.
- **No Third-Party Sharing**: Data is kept entirely within your local SQLite instance and never shared with external parties.
- **Data Deletion Support**: Fully compliant with Meta data deletion requirements via `/data-deletion`.

---

## License

Distributed under the MIT License.
