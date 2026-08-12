# FEDERaiDE

FEDERaiDE is a cross-platform, terminal-based **peer to peer** AI orchestration harness and universal automation system. 

It turns your terminal into an interactive workbench where you can run a specialized team of AI agents who talk to and collaborate with each other, use voice input and output, execute desktop automation commands, and much more.

---
<p align="center">
  <img src="https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/landing/assets/federaide_logo.webp" alt="FEDERaiDE Logo" width="100%" />
</p>

## What Makes FEDERaiDE Different?

### 1. Orchestration Freedom
You organize your team of agents **simply by writing their backstories**. Describe how they relate to one another, and FEDERaiDE builds the communication network dynamically. You can create:
* **A true Peer-to-Peer hierarchy where every agent is equal to every other agent.** This is the default and exclusive to FEDERaiDE. No other agentic system does this as of date (2nd July 2026). We are the first to implement it (though many will likely follow suit).
* A strict corporate hierarchy or military style chain-of-command.
* A collaborative Hub-and-Spoke swarm.
* Anything else you can think of.

FEDERaiDE supports a true multi-model ecosystem. Because it abstracts the underlying LLM provider, you can bring different models into the same session. You can easily watch **ChatGPT and Gemini debate** a technical architecture decision, a code optimization, or a creative writing prompt directly inside your terminal window.

### 2. Active Skills
While we support markdown based skills, FEDERaiDE debuts the 'active skill' system where actual programs are stored and executed by agents on demand. This allows automation of almost anything you can think of. There is Visual Computer Usage ability for automating tasks requiring GUI interaction.

### 3. Native Desktop Vision & Speech
On desktop machines, agents can physically interact with your computer, listen for input and also talk back to you, using any model (not just those with speech understanding or synthesis baked in). Speech to text with hotword detection results in true hands free operation. Text to speech with different voice for each agent results in immerrssive role-play.

### 4. First Class support for Termux
Even on mobile, you can delegate complex multi-step coding tasks to an autonomous sub-agent. The system spawns an isolated local git worktree, writes and edits code, runs your local tests, and presents you with a clean commit diff to approve—ensuring your active development workspace remains untouched. Did you know your old Android can compile Rust?

---

## Feature Checklist

* **File and Folder attachments (`&`):** Type `&` followed by a file or directory (e.g., `&src/app.py` or `&src/`) to attach that file or folder directly into your prompt.
* **Direct Shell Passthrough (`!`):** Type `!` followed by a command (e.g., `!git diff`) to run it in your local workspace and feed the output to the AI.
* **Integrated Text IDE:** Press `F6` to instantly toggle between the chat view, a local file tree, a code editor with symbol outline, and an execution dashboard.
* **And a lot more:** Offline Persistent memory with semantic search, goal management, skills (passive and active skills, the latter again a FEDERaiDE exclusive as on date), speech to text, text to speech, deep research with illustrated pdf generation, computer use, telegram integration, task scheduling etc already baked in, more features might be added in the future.
---

## Installation

Mac/Android(Termux):
```bash
curl https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/install.sh | bash
```

Windows:
```powershell
irm https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/install.ps1 | iex
```

Linux:
```powershell
wget -O- https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/nuxinstall.sh | sh
```

Via `uv`:
```bash
uv tool install federaide
```
Via `pipx`:
```bash
pipx install federaide
```

You can also install the package via `pip`. We recommend installing with all optional extras to enable local voice/audio and IDE capabilities:
```bash
pip install "federaide[all]"
```
*(For a lightweight installation without audio or computer usage capabilities, run `pip install federaide`).*

---

## Update

Mac/Linux/Android(Termux):
```bash
curl https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/update.sh | bash
```

Windows:
```powershell
irm https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/update.ps1 | iex
```

---

## Uninstall

Mac/Linux/Android(Termux):
```bash
curl https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/uninstall.sh | bash
```

Windows:
```powershell
irm https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/uninstall.ps1 | iex
```

---

## Quickstart

Start the application from your terminal:

```bash
federaide
```
*(You can also open the last available chat using federaide -r)*

### Basic Setup
1. On the first run of FEDERaiDE, you will be presented with an onboarding popup that will help you setup your first agent.
2. Set up your active agent, including your API keys and model choices (FEDERaiDE is pre-configured for OpenRouter, but works with any OpenAI-compatible API). 
3. FEDERaiDE will securely encrypt and save your credentials inside your native OS keychain.

### Changing Safety Modes (`Ctrl+T`)
To protect your workspace, the system boots in **SAFE** mode. You can use /tools command to check what tools are available and activated at any time for the current agent.
* Press **`Ctrl+T`** to cycle permissions:
  * **SAFE:** Agents can only call basic tools (web search, deep research and list files). You can authorize more tools in safe mode on a per-agent basis but they will still present a confirmation popup.
  * **SEMI-AUTO:** Agents can edit and execute, but FEDERaiDE will present a popup asking you to approve every single tool execution.
  * **FULL-AUTO:** Agents can run any and every tool available. No confirmation will be required.

### Command Reference
* **Attach Files and Folders:** Type `&` followed by the file path (e.g., `&src/main.py`). Use **`UP/DOWN`** arrow keys to cycle through autocomplete suggestions.
* **Mention Agents:** Type `@` followed by the agent name to route your message to a specific agent. Use `@team` to broadcast to everyone, or `@room` to talk to agents active in the current session. **Agents can also use @ to invoke other agents,** which is the core of the peer to peer system.
* **Slash Commands:** Type / followed by the command. For /help the full list of available commands and their descriptions.
* **Key Bindings:**
  * **`F2`**: Session Manager (Create a new chat or load historical multi-agent sessions)
  * **`F4`**: Open the Active Agent's configuration
  * **`F5`**: Change Agent and set the default agent
  * **`F6`**: Cycle through UI panels (Chat ↔ IDE Editor ↔ Executions Dashboard)
  * **`F8`**: Change your workspace directory
  * **`Ctrl+K`**: Start fresh conversation
  * **`Ctrl+A`**: **ABORT** (Emergency stop for any running AI tasks or terminal commands)
  * **`Ctrl+Q`**: Quit FEDERaiDE

### Disclaimer
* All AI systems inherently navigate a trade-off between capability and safety; **FEDERaiDE leans heavily toward the capability aspect.** For safe and secure coding with AI assistance, please consider using [***KittySuite IDE***](https://www.rocklab.in/kittysuite), where the AI does not have the capability to execute code, run commands, or read files you don't explicitly share.

## Roadmap & Upcoming Features:
  * Image generation (much requested feature)
  * Email integration (check inbox/read email/write email)
  * Integrated Secrets Management for Active Skills
  
## Why We Built FEDERaiDE

In many regions of the developing world, owning a desktop computer is often a luxury, a lot more people have access to smartphones. FEDERaiDE is written to bring the joy of programming to as many people as possible, by being as device agnostic as possible. It is designed to run natively and comfortably inside **Termux on Android**, transforming a spare smartphone into a fully capable engineering workstation.

FEDERaiDE is also built for advanced power-users on high-end hardware. It runs on **any Mac or PC** you can throw at it, unlocking system-level features like desktop automation, local speech-to-text with hotword detection, and text-to-speech voice generation. 

It is designed to be accessible by everyone, while being incredibly powerful.

---