"""YouTube OAuth token yenileme — console modunda çalışır (headless sunucu)."""
from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube"]
CLIENT_SECRET = "credentials/client_secret.json"
TOKEN_PATH = "credentials/token.json"

print("=" * 60)
print("YouTube OAuth Yenileme")
print("=" * 60)
print()

flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
creds = flow.run_console()

Path(TOKEN_PATH).write_text(creds.to_json())
print()
print(f"✓ Token kaydedildi: {TOKEN_PATH}")
print("Artık daemon'ı yeniden başlatabilirsiniz.")
