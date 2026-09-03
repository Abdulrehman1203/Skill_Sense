# Local Webhook Testing with ngrok

Clerk sends webhook events (e.g. `user.created`) to a **public HTTPS URL**.
During local development, [ngrok](https://ngrok.com) creates a secure tunnel
from a public URL to your `localhost:8000` Django server so Clerk can reach it.

---

## Prerequisites

| Requirement | How to get it |
|---|---|
| **ngrok account** (free) | Sign up at [dashboard.ngrok.com](https://dashboard.ngrok.com) |
| **ngrok installed** | Windows: `choco install ngrok` or `winget install ngrok` — or download from [ngrok.com/download](https://ngrok.com/download) |
| **ngrok authtoken** | Copy from [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken) |

After installing, authenticate once:

```powershell
ngrok config add-authtoken <YOUR_TOKEN>
```

Verify installation:

```powershell
ngrok version
```

---

## Quick Start

### 1. Start Django

```powershell
python manage.py runserver
# → http://127.0.0.1:8000/
```

### 2. Start ngrok (in a separate terminal)

**Option A — Ephemeral URL** (changes every restart):

```powershell
ngrok http 8000
```

**Option B — Static domain** (recommended, never changes):

```powershell
ngrok http 8000 --url=YOUR_SUBDOMAIN.ngrok-free.app
```

> **Tip**: Every free ngrok account gets **one free static domain**. Claim yours
> at [dashboard.ngrok.com/domains](https://dashboard.ngrok.com/domains). Using a
> static domain means you only configure Clerk's webhook URL **once** instead of
> re-pasting it every dev session.

ngrok will display something like:

```
Forwarding   https://abcd-1234.ngrok-free.app → http://localhost:8000
```

### 3. Your webhook endpoint URL

Append the Django route to the ngrok URL:

```
https://abcd-1234.ngrok-free.app/api/auth/clerk/webhook/
```

> **Important**: The trailing slash is required — Django's `APPEND_SLASH` will
> issue a 301 redirect without it, which drops the POST body.

---

## Wire into Clerk Dashboard

1. Go to **[Clerk Dashboard](https://dashboard.clerk.com)** → **Developers** → **Webhooks**
2. Click **Add Endpoint**
3. Set the **Endpoint URL** to your ngrok webhook URL:
   ```
   https://YOUR_SUBDOMAIN.ngrok-free.app/api/auth/clerk/webhook/
   ```
4. Subscribe to events: `user.created`, `user.updated`, `user.deleted`
5. Click **Create**
6. Copy the **Signing Secret** (`whsec_...`) from the endpoint details page

### Add the signing secret to `.env`

```env
CLERK_WEBHOOK_SECRET=whsec_your_signing_secret_here
```

> **Note**: You only need to do the signing secret step **once**. If you're using
> an ephemeral ngrok URL, you'll need to update the Endpoint URL in Clerk's
> dashboard each time ngrok restarts with a new URL.

---

## Django Configuration (already applied)

The following settings have been configured for ngrok compatibility:

### `ALLOWED_HOSTS`

```python
# settings.py
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '.ngrok-free.app']
```

The leading dot matches any `*.ngrok-free.app` subdomain. Without this, Django
returns `400 Bad Request (DisallowedHost)` before the request reaches the view.

### `CSRF_TRUSTED_ORIGINS`

```python
# settings.py
CSRF_TRUSTED_ORIGINS = [
    # ... existing origins ...
    "https://*.ngrok-free.app",
]
```

DRF `APIView` is CSRF-exempt by default (no `SessionAuthentication`), but this
is included as defensive future-proofing.

### Webhook view (`ClerkWebhookView`)

- ✅ Uses `permission_classes = [AllowAny]` — no auth required
- ✅ Reads `request.body` (raw bytes) before JSON parsing — HMAC verification works correctly
- ✅ Extends DRF `APIView` — automatically CSRF-exempt for non-session auth

---

## Verification Checklist

### Trigger a real event

1. **Sign up a test user** through your Clerk-hosted sign-up page or the app
2. This fires a `user.created` webhook event to your endpoint

### Check Clerk's side

1. Go to **Clerk Dashboard** → **Developers** → **Webhooks** → your endpoint
2. Click **Logs** tab
3. You should see a delivery with a **200** status code
4. If it failed, click the log entry to inspect the payload and error

### Check Django's side

Look for these log lines in your Django terminal:

```
INFO  Received valid Clerk Webhook event: user.created
INFO  Clerk Webhook: Created user record for clerk_id=user_xxx (email=test@example.com, role=CANDIDATE).
```

### Confirm the database

```powershell
python manage.py shell
```

```python
from interview_system.models import User
User.objects.filter(clerk_id="user_xxx")  # replace with actual clerk_id
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Clerk logs show **400** | Django's `ALLOWED_HOSTS` rejecting the ngrok domain | Ensure `'.ngrok-free.app'` is in `ALLOWED_HOSTS` |
| Clerk logs show **401** | Signature verification failed | Check `CLERK_WEBHOOK_SECRET` in `.env` matches the `whsec_...` from Clerk dashboard. Restart Django after changing `.env`. |
| Clerk logs show **403** | CSRF middleware blocking the request | Verify `ClerkWebhookView` extends DRF `APIView` (not a plain Django view). Ensure no `SessionAuthentication` in DRF config. |
| Clerk logs show **404** | Wrong URL path | The correct path is `/api/auth/clerk/webhook/` — note the trailing slash |
| Clerk logs show **500** | `CLERK_WEBHOOK_SECRET` is empty | Set `CLERK_WEBHOOK_SECRET=whsec_...` in `.env` and restart Django |
| ngrok shows **502 Bad Gateway** | Django dev server isn't running | Start Django with `python manage.py runserver` first |
| No logs on either side | Webhook endpoint not created / wrong URL in Clerk | Double-check the endpoint URL in Clerk dashboard matches your ngrok URL |

---

## Session Workflow (TL;DR)

Every time you start a dev session that needs webhooks:

```powershell
# Terminal 1: Django
python manage.py runserver

# Terminal 2: ngrok
ngrok http 8000 --domain=YOUR_SUBDOMAIN.ngrok-free.app
```

If using a **static domain**: you're done — Clerk already has the right URL.

If using an **ephemeral URL**: copy the new `https://xxxx.ngrok-free.app` URL,
go to Clerk Dashboard → Webhooks → your endpoint → edit the URL.
