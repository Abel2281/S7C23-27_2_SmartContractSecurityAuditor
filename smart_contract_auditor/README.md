# Smart Contract Security Auditor (ChainGuard)

Multi-agent LLM smart contract vulnerability auditor. Terminal-native CLI, Slither-grounded, Prosecutor/Defender/Judge consensus pipeline.

See `ARCHITECTURE.md` for full system design.

## Setup

```bash
python -m venv .venv
.venv-win\Scripts\Activate.ps1 (for windows) | source .venv/bin/activate (for linux)
pip install -r requirements.txt
cp .env.example .env   # then fill in your API keys
```

## Usage

```bash
smart-audit run ./contracts/Vault.sol
```
