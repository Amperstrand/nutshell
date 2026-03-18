# Cashu Nutshell

Chaumian ecash wallet and mint for Bitcoin Lightning based on the Cashu protocol.

## Development Setup

This is a Python project using Poetry for dependency management.

```bash
# Install dependencies
poetry install

# Install dev dependencies (for testing)
poetry install --with dev

# Activate virtual environment
poetry shell
```

## Commands

- **Format/Lint**: `poetry run ruff check . --fix`
- **Typecheck**: `poetry run mypy cashu`
- **Tests**: `poetry run pytest tests`
- **Run wallet**: `poetry run cashu`
- **Run mint**: `poetry run mint`

## Project Structure

- `cashu/` - Main package
  - `wallet/` - Wallet CLI and library
  - `mint/` - Mint server
  - `core/` - Core protocol implementations
- `tests/` - Test suite

## Configuration

Copy `.env.example` to `.env` and configure as needed.

For testing without Lightning:
```
MINT_BACKEND_BOLT11_SAT=FakeWallet
TOR=FALSE
```

## Key NUTs (Protocol Specs)

- NUT-10: Programmable ecash (P2PK, HTLCs)
- NUT-12: DLEQ proofs
- NUT-13: Deterministic wallet
- NUT-19: Caching with Redis
- NUT-21/22: Authentication

## Before Committing

1. Run `poetry run ruff check . --fix`
2. Run `poetry run mypy cashu`
3. Run `poetry run pytest tests`
