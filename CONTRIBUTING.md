# Contributing

## Dev workflow

```mermaid
sequenceDiagram
    participant Dev
    participant Repo as Local repo
    participant Hook as pre-commit hook
    participant GH as GitHub
    participant CI

    Dev->>Repo: clone / pull
    Dev->>Repo: mise install
    Dev->>Repo: mise run hooks:install
    Dev->>Repo: write code
    opt deps changed
        Dev->>Repo: mise run compile
    end
    Dev->>Repo: mise run lint
    Dev->>Repo: git commit
    Repo->>Hook: run automatically
    alt bad message or lint fail
        Hook-->>Dev: reject, back to write code
    else ok
        Dev->>GH: git push
        GH->>CI: lint job
        alt lint fail
            CI-->>Dev: fix & push again
        else lint pass
            CI->>CI: build job (amd64 + arm64)
            CI->>GH: publish GHCR YYYY.MM.DD + latest
        end
    end
```

## Conventional commits

Commit messages must follow [Conventional Commits](https://www.conventionalcommits.org/). The pre-commit hook enforces this locally; CI enforces it on every push.

Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`

Examples:

```
feat: add object storage usage scan
fix: resolve f-string backslash syntax error
chore: repin requirements
```
