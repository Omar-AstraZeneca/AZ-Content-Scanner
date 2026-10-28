"""
Registro en memoria de escaneos cancelados.

La cancelación es cooperativa: el endpoint marca el run como cancelado y el
pipeline lo comprueba entre páginas, así que el escaneo se detiene en cuanto
termina la página en curso (no en mitad de una petición).
Al vivir en memoria, se pierde si el servidor se reinicia, lo cual es correcto:
al arrancar no hay ningún escaneo en ejecución.
"""
import threading

_lock = threading.Lock()
_cancelled: set[int] = set()


def request_cancel(run_id: int) -> None:
    with _lock:
        _cancelled.add(run_id)


def is_cancelled(run_id: int) -> bool:
    with _lock:
        return run_id in _cancelled


def clear(run_id: int) -> None:
    with _lock:
        _cancelled.discard(run_id)