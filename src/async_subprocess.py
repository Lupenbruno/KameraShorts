"""FFmpeg komutlarını asyncio ile çalıştırmak için yardımcı modül."""
import asyncio
import logging

log = logging.getLogger("kamerashorts")


async def run_ffmpeg(cmd: list[str], timeout: float | None = None) -> tuple[int, bytes, bytes]:
    """
    FFmpeg komutunu async subprocess olarak çalıştırır.
    Döner: (returncode, stdout, stderr)
    Timeout aşılırsa: asyncio.TimeoutError fırlatır ve process'i öldürür.

    NOT: ffmpeg -i (probe) komutu her zaman returncode=1 döner ama
    stderr içinde medya bilgisini taşır — returncode'a bakma, stderr'e bak.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout, stderr
    except asyncio.TimeoutError:
        log.warning(
            f"FFmpeg timeout ({timeout}s), process sonlandırılıyor: "
            f"{' '.join(str(c) for c in cmd[:4])}..."
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()  # Zombie önle
        raise
