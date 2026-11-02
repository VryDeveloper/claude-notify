"""Popup do Claude Code no canto superior direito da tela.

Chamado pelos hooks do plugin com um modo e o JSON do hook na entrada padrão:

  permissao  PermissionRequest: Permitir / Sempre / Negar (com motivo opcional)
  fim        Stop: avisa que terminou e aceita uma resposta rápida
  pergunta   PreToolUse do AskUserQuestion: avisa que há uma pergunta no terminal
  aviso      Notification: avisos gerais do Claude Code

Regra de ouro: qualquer erro termina calado com código 0 e sem saída, e o
Claude segue com o fluxo normal do terminal. O plugin nunca pode travar o Claude.

Configuração por variáveis de ambiente:
  CLAUDE_AVISOS_TEMPO     segundos até o popup fechar sozinho (padrão 900)
  CLAUDE_AVISOS_SOM       0 para não tocar o sino
  CLAUDE_AVISOS_RESPOSTA  0 para o aviso de "terminei" não esperar resposta
  CLAUDE_AVISOS_DESLIGAR  1 para desligar tudo
  CLAUDE_AVISOS_SEMPRE    1 para avisar mesmo com a janela do Claude em foco

No Windows o popup só aparece quando a janela do Claude (o Claude Desktop ou o
terminal onde o Claude Code roda) não está em foco. Em foco, o hook sai calado
e o pedido aparece no lugar de sempre.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

LARGURA = 380
MARGEM = 16
ESPACO = 10
PASTA_VAGAS = os.path.join(tempfile.gettempdir(), "claude-avisos")
VAGA_VELHA = 2 * 60 * 60  # vaga sem dono há mais de 2 h é lixo de um popup morto

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


def encurtar(texto: str, limite: int) -> str:
    texto = " ".join(str(texto).split()) if limite < 200 else str(texto).strip()
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def projeto(dados: dict) -> str:
    cwd = dados.get("cwd") or os.getcwd()
    return os.path.basename(os.path.normpath(cwd)) or cwd


# ---------------------------------------------------------------- conteúdo


def detalhe_ferramenta(nome: str, entrada: dict) -> str:
    if not isinstance(entrada, dict):
        return encurtar(json.dumps(entrada, ensure_ascii=False), 400)
    if nome == "Bash":
        return encurtar(entrada.get("command", ""), 400)
    for chave in ("file_path", "notebook_path", "url", "path", "pattern", "query"):
        if entrada.get(chave):
            return encurtar(entrada[chave], 400)
    if nome == "ExitPlanMode" and entrada.get("plan"):
        return encurtar(entrada["plan"], 400)
    return encurtar(json.dumps(entrada, ensure_ascii=False, indent=1), 400)


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


def algum_popup_aberto() -> bool:
    try:
        return bool(os.listdir(PASTA_VAGAS))
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
        tempo: int = 900,
    ) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        self.tk = tk
        self.resultado: tuple[str | None, str] = (None, "")
        self.acao_enter = acao_enter
        self.vaga = Vaga()

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
        tk.Label(
            topo, text="●", fg=CORES["laranja"], bg=CORES["fundo"], font=f_txt
        ).pack(side="left")
        tk.Label(
            topo,
            text=f" Claude Code · {projeto(dados)}",
            fg=CORES["fraco"],
            bg=CORES["fundo"],
            font=f_peq,
        ).pack(side="left")
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

        tk.Label(
            caixa,
            text=titulo,
            fg=CORES["texto"],
            bg=CORES["fundo"],
            font=f_tit,
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(8, 2))
        if corpo:
            tk.Label(
                caixa,
                text=corpo,
                fg=CORES["texto"],
                bg=CORES["fundo"],
                font=f_txt,
                wraplength=LARGURA - 44,
                anchor="w",
                justify="left",
            ).pack(fill="x")
        if codigo:
            tk.Label(
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
            ).pack(fill="x", pady=(6, 0))

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

        raiz.bind("<Escape>", lambda _e: self.terminar(None))
        raiz.protocol("WM_DELETE_WINDOW", lambda: self.terminar(None))

        raiz.update_idletasks()
        altura = raiz.winfo_reqheight()
        x = raiz.winfo_screenwidth() - LARGURA - MARGEM
        y = self.vaga.posicionar(altura + 2)
        raiz.geometry(f"{LARGURA}x{altura}+{x}+{y}")
        raiz.deiconify()
        raiz.lift()
        if os.environ.get("CLAUDE_AVISOS_SOM", "1") != "0":
            raiz.bell()
        self._contar()

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
        m, s = divmod(self.restante, 60)
        self.relogio.configure(
            text=f"fecha sozinho em {m}:{s:02d} · Esc fecha e volta ao terminal"
        )
        self.restante -= 1
        self.raiz.after(1000, self._contar)

    def texto(self) -> str:
        if self.entrada is None or self.com_ajuda:
            return ""
        return self.entrada.get().strip()

    def terminar(self, acao: str | None) -> None:
        if acao == "enviar" and not self.texto():
            self._focar()
            return
        self.resultado = (acao, self.texto())
        self.vaga.soltar()
        self.raiz.destroy()

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
        corpo += f": {encurtar(entrada['description'], 160)}"
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
    resumo = encurtar(ultima_mensagem(dados), 280) or "Terminei o que você pediu."
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
            f"• {encurtar(p.get('question', ''), 140)}"
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
        dados = {**dados, "title": "Estou esperando você"}
        mensagem = "Terminei e estou esperando sua resposta no Claude."
    elif tipo == "permission_prompt":
        dados = {**dados, "title": "Preciso da sua permissão"}
        mensagem = f"{mensagem}\nResponda na janela do Claude."
    Popup(
        dados,
        dados.get("title") or "Aviso",
        encurtar(mensagem, 280),
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
        bruto = sys.stdin.read()
        dados = json.loads(bruto) if bruto.strip() else {}
        segundo_plano = False

    tempo = max(10, env_int("CLAUDE_AVISOS_TEMPO", 900))

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
        sys.stdout.write(json.dumps(saida, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(principal(sys.argv[1:]))
    except Exception:  # noqa: BLE001 — nunca travar o Claude: sem saída, ele segue no terminal
        sys.exit(0)
