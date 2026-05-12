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

# Headless sunucu için — URL'yi yazdır, kullanıcı kodu yapıştırsın
flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
auth_url, _ = flow.authorization_url(prompt="consent")
print(f"\nBu URL'yi tarayıcında aç:\n\n{auth_url}\n")
code = input("Google'ın verdiği kodu buraya yapıştır: ").strip()
flow.fetch_token(code=code)
creds = flow.credentials

Path(TOKEN_PATH).write_text(creds.to_json())
print()
print(f"✓ Token kaydedildi: {TOKEN_PATH}")
print("Artık daemon'ı yeniden başlatabilirsiniz.")
