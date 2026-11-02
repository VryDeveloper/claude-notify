# avisos-popup

Plugin do Claude Code que abre um popup no **canto superior direito da tela** quando o Claude:

- **pede permissão** → botões **Permitir**, **Negar** (com motivo opcional) e **Sempre** (quando o Claude Code oferece "sempre permitir");
- **termina o trabalho** → mostra o resumo da última mensagem e um campo de **resposta rápida**: o que você digitar vira a próxima instrução e ele continua trabalhando;
- **faz uma pergunta** (AskUserQuestion) → avisa para você voltar ao terminal;
- manda outros avisos do Claude Code.

Assim você dá alt+tab, faz outra coisa e é chamado quando precisar.

O popup entra deslizando, fica flutuando de leve (para quando o mouse está em cima), toca um som próprio (`assets/aviso.wav`) e mostra só o começo da mensagem, para não tomar a tela. **Clicar no popup** (fora dos botões e do campo) leva você para a janela do Claude.

**Só quando você não está olhando.** No Windows, se a janela do Claude (o Claude Desktop ou o terminal onde o Claude Code roda) estiver em foco, o popup não aparece e o pedido fica no lugar de sempre. Se você sair da janela depois, o aviso de "estou esperando você" do Claude Code vira um popup.

## Onde funciona

- **Claude Code no terminal** do seu PC: sim.
- **Claude Desktop, sessões de código locais** (aba Code rodando no seu PC): sim, testado. Elas usam o Claude Code do seu PC e os plugins instalados no seu usuário.
- **Sessões na nuvem** (Claude Code na web ou sessões remotas): não, o hook roda no servidor, não no seu PC.
- **Conversas normais do Claude** (aba Chat): não, elas não têm hooks.

## Instalar

Precisa de Python 3 com tkinter (já vem no instalador oficial do Python no Windows e no macOS; no Linux: `sudo apt install python3-tk`).

Dentro do Claude Code:

```
/plugin install avisos-popup --marketplace VryDeveloper/claude-notify
```

Ou, para testar a partir de um clone local:

```bash
claude --plugin-dir .
```

## Como funciona

São hooks do Claude Code (`hooks/hooks.json`) que chamam `scripts/avisar.py`:

| Evento | O que o popup faz | Volta para o Claude |
|---|---|---|
| `PermissionRequest` | Permitir / Sempre / Negar | a decisão; se você fechar (✕ ou Esc) ou o tempo acabar, o pedido normal aparece no terminal |
| `Stop` | resumo + resposta rápida | sua resposta como próxima instrução; Ok/Esc só fecha |
| `PreToolUse` (AskUserQuestion) | avisa da pergunta | nada (não bloqueia) |
| `Notification` | aviso simples (ex.: "estou esperando você"), se não houver outro popup aberto | nada (não bloqueia) |

Vários popups (de várias sessões) se empilham sem sobrepor. Qualquer erro, ou falta de Python/tkinter, faz o script sair calado: o Claude segue normal no terminal.

**Atenção:** quando você está fora da janela, enquanto um popup de permissão ou de "terminei" está aberto, o terminal espera por ele. Se voltou para o terminal, aperte **Esc** no popup (ou clique no ✕) para responder por lá.

## Configurar

Variáveis de ambiente (no shell ou no `env` do `settings.json` do Claude Code):

| Variável | Padrão | Efeito |
|---|---|---|
| `CLAUDE_AVISOS_TEMPO` | `20` | segundos até o popup fechar sozinho (de 5 a 20) |
| `CLAUDE_AVISOS_SOM` | `1` | `0` desliga o som |
| `CLAUDE_AVISOS_SOM_ARQUIVO` | — | caminho de um `.wav` seu para tocar no lugar do som do plugin |
| `CLAUDE_AVISOS_LIMITE` | `140` | máximo de caracteres da mensagem mostrados no popup |
| `CLAUDE_AVISOS_RESPOSTA` | `1` | `0` faz o "terminei" ser só um aviso, sem campo de resposta e sem segurar o terminal |
| `CLAUDE_AVISOS_DESLIGAR` | `0` | `1` desliga tudo |
| `CLAUDE_AVISOS_SEMPRE` | `0` | `1` avisa mesmo com a janela do Claude em foco |
| `CLAUDE_AVISOS_PYTHON` | — | caminho do Python a usar, se o automático não achar |

## Som

O som é gerado por `assets/gerar_som.py` (duas notas curtas). Para mudar, edite as notas no script e rode `python assets/gerar_som.py`, ou aponte `CLAUDE_AVISOS_SOM_ARQUIVO` para outro `.wav`.
