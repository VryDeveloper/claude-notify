"""Gera assets/aviso.wav: um "plim" curto de duas notas, macio e sem clique.

Rode de novo para ajustar: python assets/gerar_som.py
"""

import math
import os
import struct
import wave

TAXA = 44100
DURACAO = 0.7
VOLUME = 0.55
# (frequência em Hz, início em segundos, decaimento)
NOTAS = [(1046.5, 0.00, 7.0), (1568.0, 0.09, 6.0)]


def amostra(t: float) -> float:
    valor = 0.0
    for freq, inicio, decai in NOTAS:
        dt = t - inicio
        if dt < 0:
            continue
        ataque = min(1.0, dt / 0.006)
        envelope = ataque * math.exp(-decai * dt)
        # fundamental + um pouco de oitava para soar como sininho
        valor += envelope * (
            math.sin(2 * math.pi * freq * dt)
            + 0.25 * math.sin(2 * math.pi * 2 * freq * dt)
        )
    fim = max(0.0, (DURACAO - t) / 0.05)  # some suave no final
    return VOLUME * min(1.0, fim) * valor / len(NOTAS)


def main() -> None:
    destino = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aviso.wav")
    total = int(TAXA * DURACAO)
    with wave.open(destino, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        quadros = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, amostra(i / TAXA))) * 32767))
            for i in range(total)
        )
        w.writeframes(quadros)
    print(destino)


if __name__ == "__main__":
    main()
