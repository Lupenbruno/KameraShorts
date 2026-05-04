"""Windows Task Scheduler'a AsfaltTV'yi ekler — PC açılınca otomatik başlar."""
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
PYTHON      = PROJECT_DIR / "venv" / "Scripts" / "python.exe"
SCRIPT      = PROJECT_DIR / "dashboard.py"
TASK_NAME   = "AsfaltTV_Dashboard"

def install():
    # Task Scheduler XML komutu
    cmd = [
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAME,
        "/TR", f'"{PYTHON}" "{SCRIPT}"',
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/DELAY", "0000:30",   # Oturum açıldıktan 30 saniye sonra başlat
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("BASARILI: AsfaltTV artik PC acilinca otomatik basliyor.")
        print(f"Gorev adi: {TASK_NAME}")
    else:
        print(f"HATA: {result.stderr}")
        print("Yonetici olarak calistirmayi deneyin.")

def uninstall():
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("Otomatik baslama kaldirildi.")
    else:
        print(f"HATA: {result.stderr}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        uninstall()
    else:
        install()
