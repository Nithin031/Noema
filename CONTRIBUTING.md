# Contributing to Noema

Noema development centers on the `noema` package and its tests.
Keep changes focused, preserve privacy boundaries, and do not add heuristic
classification or silently discard observed activity.

## Checks

Run the Python suite:

```powershell
python -m pytest tests -q
```

Build each frontend package from its own directory when changing its source:

```powershell
Push-Location web
npm run build
Pop-Location
```

Use environment variables for credentials. Never commit `.env` or API keys.
Preserve `LICENSE.txt` and `CITATION.cff` when redistributing the project.
