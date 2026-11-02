"""Popup do Claude Code no canto superior direito da tela.

Chamado pelos hooks do plugin com um modo e o JSON do hook na entrada padrão:

  permissao  PermissionRequest: Permitir / Sempre / Negar (com motivo opcional)
  fim        Stop: avisa que terminou e aceita uma resposta rápida
  pergunta   PreToolUse do AskUserQuestion: avisa que há uma pergunta no terminal
  aviso      Notification: avisos gerais do Claude Code

Regra de ouro: qualquer erro termina calado com código 0 e sem saída, e o
Claude segue com o fluxo normal do terminal. O plugin nunca pode travar o Claude.

Configuração por variáveis de ambiente:
  CLAUDE_AVISOS_TEMPO     segundos até o popup fechar sozinho (padrão 20, máximo 20)
  CLAUDE_AVISOS_SOM       0 para não tocar som
  CLAUDE_AVISOS_SOM_ARQUIVO  caminho de um .wav para usar no lugar do som do plugin
  CLAUDE_AVISOS_LIMITE    máximo de caracteres da mensagem no popup (padrão 140)
  CLAUDE_AVISOS_RESPOSTA  0 para o aviso de "terminei" não esperar resposta
  CLAUDE_AVISOS_DESLIGAR  1 para desligar tudo
  CLAUDE_AVISOS_SEMPRE    1 para avisar mesmo com a janela do Claude em foco

No Windows o popup só aparece quando a janela do Claude (o Claude Desktop ou o
terminal onde o Claude Code roda) não está em foco. Em foco, o hook sai calado
e o pedido aparece no lugar de sempre.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

LARGURA = 380
MARGEM = 16
ESPACO = 10
RAIZ_PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOM_PADRAO = os.path.join(RAIZ_PLUGIN, "assets", "aviso.wav")
PASTA_VAGAS = os.path.join(tempfile.gettempdir(), "claude-avisos")
VAGA_VELHA = (
    60  # popup dura no máximo 20 s; vaga mais velha que isso é de um popup morto
)

CORES = {
    "fundo": "#1f1e1d",
    "borda": "#3a3836",
    "texto": "#ece9e4",
    "fraco": "#a19d97",
    "codigo": "#2b2a28",
    "laranja": "#d97757",
    "verde": "#4f9d69",
    "vermelho": "#c0574f",
    "neutro": "#3d3b38",
}


def env_int(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def limite() -> int:
    """Quantos caracteres da mensagem cabem no popup (cerca de 3 linhas)."""
    return max(40, env_int("CLAUDE_AVISOS_LIMITE", 140))


def encurtar(texto: str, limite: int) -> str:
    texto = " ".join(str(texto).split()) if limite < 200 else str(texto).strip()
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def projeto(dados: dict) -> str:
    cwd = dados.get("cwd") or os.getcwd()
    return os.path.basename(os.path.normpath(cwd)) or cwd


# ---------------------------------------------------------------- conteúdo


def detalhe_ferramenta(nome: str, entrada: dict) -> str:
    if not isinstance(entrada, dict):
        return encurtar(json.dumps(entrada, ensure_ascii=False), 180)
    if nome == "Bash":
        return encurtar(entrada.get("command", ""), 180)
    for chave in ("file_path", "notebook_path", "url", "path", "pattern", "query"):
        if entrada.get(chave):
            return encurtar(entrada[chave], 180)
    if nome == "ExitPlanMode" and entrada.get("plan"):
        return encurtar(entrada["plan"], 180)
    return encurtar(json.dumps(entrada, ensure_ascii=False, indent=1), 180)


def ultima_mensagem(dados: dict) -> str:
    """Texto final do Claude: do próprio hook ou, se faltar, do transcript."""
    texto = dados.get("last_assistant_message")
    if isinstance(texto, str) and texto.strip():
        return texto
    caminho = dados.get("transcript_path")
    if not caminho or not os.path.exists(caminho):
        return ""
    try:
        with open(caminho, "rb") as f:
            f.seek(0, os.SEEK_END)
            tamanho = f.tell()
            f.seek(max(0, tamanho - 512 * 1024))
            linhas = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for linha in reversed(linhas):
        try:
            item = json.loads(linha)
        except ValueError:
            continue
        if item.get("type") != "assistant":
            continue
        conteudo = (item.get("message") or {}).get("content")
        if isinstance(conteudo, str) and conteudo.strip():
            return conteudo
        if isinstance(conteudo, list):
            partes = [
                c.get("text", "")
                for c in conteudo
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            texto = "\n".join(p for p in partes if p.strip())
            if texto:
                return texto
    return ""


# ---------------------------------------------------------------- foco


def _processos_windows() -> tuple[dict[int, str], dict[int, int]]:
    """Nome e pai de cada processo, pela Toolhelp do Windows."""
    import ctypes
    from ctypes import wintypes

    class Entrada(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entrada)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entrada)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    nomes: dict[int, str] = {}
    pais: dict[int, int] = {}
    foto = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not foto or foto == wintypes.HANDLE(-1).value:
        return nomes, pais
    try:
        e = Entrada()
        e.dwSize = ctypes.sizeof(Entrada)
        ok = k32.Process32FirstW(foto, ctypes.byref(e))
        while ok:
            nomes[e.th32ProcessID] = e.szExeFile
            pais[e.th32ProcessID] = e.th32ParentProcessID
            ok = k32.Process32NextW(foto, ctypes.byref(e))
    finally:
        k32.CloseHandle(foto)
    return nomes, pais


def claude_em_foco() -> bool:
    """True quando a janela em primeiro plano é a do Claude.

    Conta como janela do Claude: o Claude Desktop, ou a janela de qualquer
    processo acima deste na árvore (o terminal onde o Claude Code roda).
    Só no Windows; nos outros sistemas, e em qualquer erro, devolve False e o
    popup aparece.
    """
    if sys.platform != "win32" or os.environ.get("CLAUDE_AVISOS_SEMPRE") == "1":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32.GetForegroundWindow.restype = wintypes.HWND
        k32.GetConsoleWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]

        janela = u32.GetForegroundWindow()
        if not janela:
            return False
        if janela == k32.GetConsoleWindow():
            return True
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(janela, ctypes.byref(pid))
        alvo = pid.value

        nomes, pais = _processos_windows()
        if nomes.get(alvo, "").lower() == "claude.exe":
            return True
        atual, vistos = os.getpid(), set()
        while atual and atual not in vistos:
            if atual == alvo:
                return True
            vistos.add(atual)
            atual = pais.get(atual, 0)
    except Exception:  # noqa: BLE001 — na dúvida, avisa
        return False
    return False


MARCA_FIM = os.path.join(tempfile.gettempdir(), "claude-avisos-ultimo-fim")


def marcar_fim() -> None:
    try:
        with open(MARCA_FIM, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def fim_recente(segundos: int = 180) -> bool:
    try:
        return time.time() - os.path.getmtime(MARCA_FIM) < segundos
    except OSError:
        return False


def tocar_som() -> None:
    """Toca o som do plugin sem travar a janela. Nunca usa o som do sistema."""
    if os.environ.get("CLAUDE_AVISOS_SOM", "1") == "0":
        return
    arquivo = os.environ.get("CLAUDE_AVISOS_SOM_ARQUIVO") or SOM_PADRAO
    if not os.path.isfile(arquivo):
        return
    try:
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(
                arquivo,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            return
        comandos = (
            [["afplay"]] if sys.platform == "darwin" else [["paplay"], ["aplay", "-q"]]
        )
        for cmd in comandos:
            if shutil.which(cmd[0]):
                subprocess.Popen(
                    [*cmd, arquivo],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
    except Exception:  # noqa: BLE001, S110 — som é enfeite, nunca motivo de erro
        pass


def focar_claude() -> bool:
    """Traz para frente a janela do Claude (terminal acima deste processo ou
    Claude Desktop). Só no Windows; devolve True se achou a janela."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetWindow.restype = wintypes.HWND
        u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsIconic.argtypes = [wintypes.HWND]
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]

        nomes, pais = _processos_windows()
        # primeiro os processos acima deste (o terminal), depois o Claude Desktop
        ordem: list[int] = []
        atual, vistos = pais.get(os.getpid(), 0), {os.getpid()}
        while atual and atual not in vistos:
            ordem.append(atual)
            vistos.add(atual)
            atual = pais.get(atual, 0)
        ordem += [
            p for p, n in nomes.items() if n.lower() == "claude.exe" and p not in vistos
        ]

        janelas: dict[int, list] = {}

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cada(h, _l):
            # janelas visíveis, de primeiro nível e com título
            if (
                u32.IsWindowVisible(h)
                and not u32.GetWindow(h, 4)
                and u32.GetWindowTextLengthW(h) > 0
            ):
                pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                janelas.setdefault(pid.value, []).append(h)
            return True

        u32.EnumWindows(cada, 0)
        for pid in ordem:
            if janelas.get(pid):
                h = janelas[pid][0]
                if u32.IsIconic(h):
                    u32.ShowWindow(h, 9)  # SW_RESTORE
                u32.SetForegroundWindow(h)
                return True
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def cantos_redondos(raiz) -> None:
    """Cantos arredondados do Windows 11 (no 10 não faz nada)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(raiz.winfo_id())
        preferencia = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(preferencia), ctypes.sizeof(preferencia)
        )
    except Exception:  # noqa: BLE001, S110
        pass


def algum_popup_aberto() -> bool:
    try:
        agora = time.time()
        return any(
            agora - os.path.getmtime(os.path.join(PASTA_VAGAS, nome)) < VAGA_VELHA
            for nome in os.listdir(PASTA_VAGAS)
        )
    except OSError:
        return False


# ---------------------------------------------------------------- vagas na tela


class Vaga:
    """Um arquivo por popup aberto, para empilhar vários sem sobrepor."""

    def __init__(self) -> None:
        self.caminho: str | None = None
        try:
            os.makedirs(PASTA_VAGAS, exist_ok=True)
            agora = time.time()
            for i in range(8):
                caminho = os.path.join(PASTA_VAGAS, f"vaga-{i}")
                try:
                    if agora - os.path.getmtime(caminho) > VAGA_VELHA:
                        os.remove(caminho)
                except OSError:
                    pass
                try:
                    fd = os.open(caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    continue
                os.close(fd)
                self.caminho = caminho
                return
        except OSError:
            pass

    def posicionar(self, altura: int) -> int:
        """Põe o popup logo abaixo dos que já estão abertos e anota onde ele termina."""
        y = MARGEM
        try:
            for nome in os.listdir(PASTA_VAGAS):
                caminho = os.path.join(PASTA_VAGAS, nome)
                if caminho == self.caminho:
                    continue
                try:
                    with open(caminho, encoding="utf-8") as f:
                        y = max(y, int(f.read().strip() or 0) + ESPACO)
                except (OSError, ValueError):
                    pass
            if self.caminho:
                with open(self.caminho, "w", encoding="utf-8") as f:
                    f.write(str(y + altura))
        except OSError:
            pass
        return y

    def soltar(self) -> None:
        if self.caminho:
            try:
                os.remove(self.caminho)
            except OSError:
                pass
            self.caminho = None


# ---------------------------------------------------------------- janela


class Popup:
    """Janela sempre no topo, no canto superior direito.

    botoes: lista de (rotulo, acao, cor). campo: texto de ajuda do campo de
    resposta, ou None para não ter campo. Devolve (acao, texto); acao é None
    quando o popup fechou sozinho ou pelo ×.
    """

    def __init__(
        self,
        dados: dict,
        titulo: str,
        corpo: str,
        codigo: str = "",
        botoes: list[tuple[str, str, str]] | None = None,
        campo: str | None = None,
        acao_enter: str | None = None,
        tempo: int = 20,
    ) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        self.tk = tk
        self.resultado: tuple[str | None, str] = (None, "")
        self.acao_enter = acao_enter
        self.vaga = Vaga()
        self.saindo = False
        self.inicio = time.time()
        clicaveis = []  # áreas que levam ao Claude quando clicadas

        raiz = self.raiz = tk.Tk()
        raiz.withdraw()
        raiz.title(f"Claude · {projeto(dados)}")
        raiz.configure(bg=CORES["borda"])
        raiz.resizable(False, False)
        if sys.platform == "win32":
            raiz.overrideredirect(True)
        raiz.attributes("-topmost", True)
        if sys.platform.startswith("linux"):
            try:
                raiz.attributes("-type", "dialog")
            except tk.TclError:
                pass

        base = tkfont.nametofont("TkDefaultFont").actual("family")
        f_peq = (base, 9)
        f_txt = (base, 10)
        f_tit = (base, 11, "bold")
        f_cod = (
            "Consolas"
            if sys.platform == "win32"
            else "Menlo"
            if sys.platform == "darwin"
            else "DejaVu Sans Mono",
            9,
        )

        caixa = tk.Frame(raiz, bg=CORES["fundo"], padx=14, pady=12)
        caixa.pack(fill="both", expand=True, padx=1, pady=1)

        topo = tk.Frame(caixa, bg=CORES["fundo"])
        topo.pack(fill="x")
        ponto = tk.Label(
            topo, text="●", fg=CORES["laranja"], bg=CORES["fundo"], font=f_txt
        )
        ponto.pack(side="left")
        rotulo_projeto = tk.Label(
            topo,
            text=f" Claude Code · {projeto(dados)}",
            fg=CORES["fraco"],
            bg=CORES["fundo"],
            font=f_peq,
        )
        rotulo_projeto.pack(side="left")
        clicaveis += [caixa, topo, ponto, rotulo_projeto]
        fechar = tk.Label(
            topo,
            text="✕",
            fg=CORES["fraco"],
            bg=CORES["fundo"],
            font=f_txt,
            cursor="hand2",
        )
        fechar.pack(side="right")
        fechar.bind("<Button-1>", lambda _e: self.terminar(None))

        rotulo_titulo = tk.Label(
            caixa,
            text=titulo,
            fg=CORES["texto"],
            bg=CORES["fundo"],
            font=f_tit,
            anchor="w",
            justify="left",
        )
        rotulo_titulo.pack(fill="x", pady=(8, 2))
        clicaveis.append(rotulo_titulo)
        if corpo:
            rotulo_corpo = tk.Label(
                caixa,
                text=corpo,
                fg=CORES["texto"],
                bg=CORES["fundo"],
                font=f_txt,
                wraplength=LARGURA - 44,
                anchor="w",
                justify="left",
            )
            rotulo_corpo.pack(fill="x")
            clicaveis.append(rotulo_corpo)
        if codigo:
            rotulo_codigo = tk.Label(
                caixa,
                text=codigo,
                fg=CORES["texto"],
                bg=CORES["codigo"],
                font=f_cod,
                wraplength=LARGURA - 46,
                anchor="w",
                justify="left",
                padx=8,
                pady=6,
            )
            rotulo_codigo.pack(fill="x", pady=(6, 0))
            clicaveis.append(rotulo_codigo)

        self.entrada = None
        if campo is not None:
            moldura = tk.Frame(caixa, bg=CORES["borda"], padx=1, pady=1)
            moldura.pack(fill="x", pady=(10, 0))
            self.entrada = tk.Entry(
                moldura,
                bg=CORES["codigo"],
                fg=CORES["texto"],
                insertbackground=CORES["texto"],
                relief="flat",
                font=f_txt,
            )
            self.entrada.pack(fill="x", ipady=5, ipadx=4)
            self.ajuda = campo
            self._mostrar_ajuda()
            self.entrada.bind("<FocusIn>", self._tirar_ajuda)
            self.entrada.bind("<Button-1>", self._focar)
            self.entrada.bind(
                "<Return>",
                lambda _e: self.acao_enter and self.terminar(self.acao_enter),
            )

        if botoes:
            linha = tk.Frame(caixa, bg=CORES["fundo"])
            linha.pack(fill="x", pady=(10, 0))
            clicaveis.append(linha)
            for rotulo, acao, cor in reversed(botoes):
                b = tk.Label(
                    linha,
                    text=rotulo,
                    fg="#ffffff",
                    bg=CORES[cor],
                    font=(base, 10, "bold"),
                    padx=12,
                    pady=5,
                    cursor="hand2",
                )
                b.pack(side="right", padx=(6, 0))
                b.bind("<Button-1>", lambda _e, a=acao: self.terminar(a))

        self.restante = tempo
        self.relogio = tk.Label(
            caixa, text="", fg=CORES["fraco"], bg=CORES["fundo"], font=f_peq, anchor="w"
        )
        self.relogio.pack(fill="x", pady=(8, 0))
        clicaveis.append(self.relogio)

        for w in clicaveis:
            w.configure(cursor="hand2")
            w.bind("<Button-1>", self.ir_para_claude)

        raiz.bind("<Escape>", lambda _e: self.terminar(None))
        raiz.protocol("WM_DELETE_WINDOW", lambda: self.terminar(None))

        raiz.update_idletasks()
        self.altura = raiz.winfo_reqheight()
        self.x_final = raiz.winfo_screenwidth() - LARGURA - MARGEM
        self.x_inicio = self.x_final + 60
        self.y_base = self.vaga.posicionar(self.altura + 2)
        self.x, self.y = self.x_inicio, self.y_base
        self._opacidade(0.0)
        self._mover()
        raiz.deiconify()
        raiz.lift()
        cantos_redondos(raiz)
        tocar_som()
        self._entrar(0)
        self._contar()

    # ------------------------------------------------------------ animação

    def _opacidade(self, valor: float) -> None:
        try:
            self.raiz.attributes("-alpha", max(0.0, min(1.0, valor)))
        except self.tk.TclError:
            pass

    def _mover(self) -> None:
        self.raiz.geometry(f"{LARGURA}x{self.altura}+{int(self.x)}+{int(self.y)}")

    def _entrar(self, passo: int, total: int = 16) -> None:
        """Desliza da direita para o lugar e aparece aos poucos."""
        if self.saindo:
            return
        p = passo / total
        suave = 1 - (1 - p) ** 3
        self.x = self.x_inicio + (self.x_final - self.x_inicio) * suave
        self._opacidade(suave)
        self._mover()
        if passo < total:
            self.raiz.after(15, self._entrar, passo + 1, total)
        else:
            self.raiz.after(40, self._flutuar)

    def _flutuar(self) -> None:
        """Sobe e desce devagar; para quando o mouse está em cima ou você digita."""
        if self.saindo:
            return
        px, py = self.raiz.winfo_pointerxy()
        mouse_em_cima = (
            self.x <= px <= self.x + LARGURA and self.y <= py <= self.y + self.altura
        )
        digitando = self.entrada is not None and not self.com_ajuda
        if not (mouse_em_cima or digitando):
            t = time.time() - self.inicio
            self.y = self.y_base + round(3 * math.sin(t * 2 * math.pi / 2.6))
            self._mover()
        self.raiz.after(40, self._flutuar)

    def _sair(self, passo: int = 0, total: int = 10) -> None:
        """Some aos poucos deslizando para a direita e fecha."""
        p = passo / total
        self.x = self.x_final + 40 * p * p
        self._opacidade(1 - p)
        self._mover()
        if passo < total:
            self.raiz.after(15, self._sair, passo + 1, total)
        else:
            self.raiz.destroy()

    def ir_para_claude(self, _e=None) -> None:
        """Clique no popup (fora dos botões e do campo): abre a janela do Claude."""
        focar_claude()
        self.terminar(None)

    def _mostrar_ajuda(self) -> None:
        self.entrada.delete(0, "end")
        self.entrada.insert(0, self.ajuda)
        self.entrada.configure(fg=CORES["fraco"])
        self.com_ajuda = True

    def _tirar_ajuda(self, _e=None) -> None:
        if self.com_ajuda:
            self.entrada.delete(0, "end")
            self.entrada.configure(fg=CORES["texto"])
            self.com_ajuda = False

    def _focar(self, _e=None) -> None:
        # janelas sem moldura no Windows só pegam o teclado se pedirmos
        self.raiz.focus_force()
        self.entrada.focus_set()
        self._tirar_ajuda()

    def _contar(self) -> None:
        if self.restante <= 0:
            self.terminar(None)
            return
        self.relogio.configure(
            text=f"clique para abrir o Claude · fecha em {self.restante}s"
        )
        self.restante -= 1
        self.raiz.after(1000, self._contar)

    def texto(self) -> str:
        if self.entrada is None or self.com_ajuda:
            return ""
        return self.entrada.get().strip()

    def terminar(self, acao: str | None) -> None:
        if self.saindo:
            return
        if acao == "enviar" and not self.texto():
            self._focar()
            return
        self.saindo = True
        self.resultado = (acao, self.texto())
        self.vaga.soltar()
        self._sair()

    def esperar(self) -> tuple[str | None, str]:
        try:
            self.raiz.mainloop()
        finally:
            self.vaga.soltar()
        return self.resultado


# ---------------------------------------------------------------- modos


def modo_permissao(dados: dict, tempo: int) -> dict | None:
    nome = dados.get("tool_name", "ferramenta")
    entrada = dados.get("tool_input") or {}
    corpo = f"Quer usar {nome}"
    if isinstance(entrada, dict) and entrada.get("description"):
        corpo += f": {encurtar(entrada['description'], 100)}"
    sugestoes = dados.get("permission_suggestions")
    botoes = [("Negar", "negar", "vermelho")]
    if sugestoes:
        botoes.append(("Sempre", "sempre", "neutro"))
    botoes.append(("Permitir", "permitir", "verde"))

    acao, texto = Popup(
        dados,
        "Pedido de permissão",
        corpo,
        detalhe_ferramenta(nome, entrada),
        botoes=botoes,
        campo="Motivo, se negar (opcional)",
        acao_enter="negar",
        tempo=tempo,
    ).esperar()
    if acao is None:
        return None  # sem decisão: o Claude mostra o pedido normal no terminal
    decisao: dict = {"behavior": "allow"}
    if acao == "sempre":
        decisao["updatedPermissions"] = sugestoes
    elif acao == "negar":
        motivo = texto or "O usuário negou pela notificação."
        decisao = {
            "behavior": "deny",
            "message": f"O usuário negou pela notificação: {motivo}"
            if texto
            else motivo,
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decisao,
        }
    }


def modo_fim(dados: dict, tempo: int) -> dict | None:
    resumo = encurtar(ultima_mensagem(dados), limite()) or "Terminei o que você pediu."
    marcar_fim()
    if os.environ.get("CLAUDE_AVISOS_RESPOSTA", "1") == "0":
        Popup(
            dados,
            "Terminei",
            resumo,
            botoes=[("Ok", "ok", "neutro")],
            tempo=min(tempo, 30),
        ).esperar()
        return None
    acao, texto = Popup(
        dados,
        "Terminei",
        resumo,
        botoes=[("Ok", "ok", "neutro"), ("Enviar", "enviar", "laranja")],
        campo="Resposta rápida (Enter envia)",
        acao_enter="enviar",
        tempo=tempo,
    ).esperar()
    if acao == "enviar" and texto:
        return {
            "decision": "block",
            "reason": f"Resposta do usuário pela notificação: {texto}",
        }
    return None


def modo_pergunta(dados: dict, tempo: int) -> None:
    entrada = dados.get("tool_input") or {}
    perguntas = entrada.get("questions") if isinstance(entrada, dict) else None
    texto = ""
    if isinstance(perguntas, list) and perguntas:
        texto = "\n".join(
            f"• {encurtar(p.get('question', ''), limite())}"
            for p in perguntas
            if isinstance(p, dict)
        )
    Popup(
        dados,
        "Tenho uma pergunta",
        texto or "Responda no terminal.",
        botoes=[("Ok", "ok", "neutro")],
        tempo=min(tempo, 120),
    ).esperar()


def modo_aviso(dados: dict, tempo: int) -> None:
    # Pedido de permissão e "esperando você" já costumam ter popup próprio
    # (PermissionRequest e Stop). Este aviso cobre quando você saiu da janela
    # depois: aparece só se não houver outro popup aberto.
    if algum_popup_aberto():
        return
    mensagem = dados.get("message", "")
    tipo = dados.get("notification_type", "")
    if tipo == "idle_prompt":
        if fim_recente():
            return  # o popup de "terminei" acabou de avisar
        dados = {**dados, "title": "Estou esperando você"}
        mensagem = "Terminei e estou esperando sua resposta no Claude."
    elif tipo == "permission_prompt":
        dados = {**dados, "title": "Preciso da sua permissão"}
        mensagem = f"{mensagem}\nResponda na janela do Claude."
    Popup(
        dados,
        dados.get("title") or "Aviso",
        encurtar(mensagem, limite()),
        botoes=[("Ok", "ok", "neutro")],
        tempo=min(tempo, 20),
    ).esperar()


def soltar_em_segundo_plano(modo: str, dados: dict) -> None:
    """Abre o popup num processo separado e devolve o hook na hora."""
    fd, caminho = tempfile.mkstemp(prefix="claude-aviso-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dados, f)
    opcoes: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        opcoes["creationflags"] = (
            0x00000008 | 0x00000200
        )  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        opcoes["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), modo, "--arquivo", caminho],
        **opcoes,
    )


def principal(argv: list[str]) -> int:
    if os.environ.get("CLAUDE_AVISOS_DESLIGAR") == "1" or not argv:
        return 0
    modo = argv[0]
    if "--arquivo" in argv:
        caminho = argv[argv.index("--arquivo") + 1]
        with open(caminho, encoding="utf-8") as f:
            dados = json.load(f)
        os.remove(caminho)
        segundo_plano = True
    else:
        # o Claude Code manda UTF-8; no Windows o padrão do Python seria cp1252
        bruto = sys.stdin.buffer.read().decode("utf-8", "replace")
        dados = json.loads(bruto) if bruto.strip() else {}
        segundo_plano = False

    tempo = min(20, max(5, env_int("CLAUDE_AVISOS_TEMPO", 20)))

    # você está olhando para o Claude: deixa o pedido aparecer no lugar de sempre
    if not segundo_plano and claude_em_foco():
        return 0

    if modo in ("pergunta", "aviso") and not segundo_plano:
        soltar_em_segundo_plano(modo, dados)
        return 0

    saida = None
    if modo == "permissao":
        saida = modo_permissao(dados, tempo)
    elif modo == "fim":
        saida = modo_fim(dados, tempo)
    elif modo == "pergunta":
        modo_pergunta(dados, tempo)
    elif modo == "aviso":
        modo_aviso(dados, tempo)

    if saida is not None:
        # só ASCII na saída: os acentos viram \\u00e1 e chegam certos em qualquer sistema
        sys.stdout.write(json.dumps(saida))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(principal(sys.argv[1:]))
    except Exception:  # noqa: BLE001 — nunca travar o Claude: sem saída, ele segue no terminal
        sys.exit(0)
