# Aether assistant

Aether is a private assistant that runs on this computer. Open it with **Super+Space** (or `aether-assistant`), chat in a terminal with `aetherctl chat`, or use the Speak button / `aetherctl listen` for voice input.

## What it can do

Plain requests are matched to tools directly, so they work even without the language model:

| Say | What happens |
| --- | --- |
| `create a folder called Projects` | Creates `~/Projects` |
| `create a note called todo with buy milk` | Creates `~/todo.txt` (never overwrites) |
| `search my files for travel plans` | Meaning-based search through `semfsd` |
| `list files in Documents`, `read file notes.txt` | Browse and preview text files |
| `open Firefox`, `open files`, `open terminal` | Launch installed apps |
| `open https://example.org` | Open a web address |
| `volume 40`, `mute`, `brightness 70`, `take a screenshot` | System controls |
| `remember that my sister's birthday is 3 May` | Stores a note you control |
| `what do you remember`, `forget 2`, `forget everything` | Review or erase memory |
| `move a.txt to Documents`, `delete old.txt` | Asks for approval first |
| `shut down`, `reboot` | Asks for approval first |
| `what time is it`, `what did you do` | Status and the action history |

Anything else goes to the local language model (Qwen2.5-1.5B-Instruct via llama.cpp). If the model proposes an action, you are asked to approve it unless it only reads information.

## Staying in control

- Works only inside your home folder and refuses secret locations such as `~/.ssh`.
- Asks before moving, deleting, erasing memory, or powering off. Deleting moves to the Trash.
- Logs every action to `~/.local/state/aether/actions.log`.
- Memory is a JSON file you can read or delete: `~/.local/share/aether/memory.json`.
- Nothing leaves the machine.

## If it says the model isn't running

`aetherctl doctor` shows the state of `aether-llm`, `aid`, and anything that failed during the build. Start the model with `sudo systemctl start aether-llm` (the first answer after boot can take a minute while it loads). Tool commands in the table above keep working without the model.

## Code

- `overlay/usr/libexec/aether/agent.py`: routing, tools, approvals, memory, audit log (no GUI; covered by `tests/test_assistant.py`).
- `overlay/usr/libexec/aether/assistant_ui.py`: the GTK 4 / libadwaita window.
