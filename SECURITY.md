# Segurança — ações operacionais obrigatórias

As correções de código já estão aplicadas. Os itens abaixo dependem de acesso a
sistemas externos e **precisam ser feitos manualmente** pela equipe.

## 1. Rotacionar segredos que já vazaram (URGENTE)

Estes valores estavam em texto plano no repositório / árvore de trabalho e devem
ser considerados comprometidos:

| Segredo | Onde estava | Ação |
|---|---|---|
| Senha do PostgreSQL de produção | `backend/.env` (`DATABASE_URL`) | Trocar a senha do banco na Railway e atualizar a env `DATABASE_URL` do serviço. |
| Chaves do bucket de backups | `backend/.env` (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) | Revogar e gerar novas credenciais no provedor de storage (Tigris/`t3.storageapi.dev`). |
| `AUTHENTICATION_API_KEY` da Evolution API | `evolutionapi/.env` (`879372`) | Já regerada localmente (chave forte). Aplicar a nova chave na instância em produção e reparear se necessário. |
| `SECRET_KEY` do Django | default embutido em `settings.py` | Definir `SECRET_KEY` (env) na Railway com valor aleatório: `python -c "import secrets; print(secrets.token_urlsafe(64))"`. O app agora **recusa subir** em produção sem ela. |

## 2. Purga do histórico git

O dump `backups/db_backup_20260513_190100.sqlite3` (hashes de senha + PII) foi
removido do índice, mas **continua no histórico** (commit `fe5c1e8`).

```bash
pipx run git-filter-repo --path backups/db_backup_20260513_190100.sqlite3 --invert-paths
# ou BFG:  bfg --delete-files 'db_backup_*.sqlite3'
git push --force --all && git push --force --tags
```

Avise quem tiver clones para re-clonar. Como o dump circulou, trate as senhas do
staff como comprometidas e force a rotação (o app já tem o fluxo
`must_change_password`).

## 3. Variáveis de ambiente de produção (Railway)

Defina explicitamente (o app agora falha fechado sem elas):

```
SECRET_KEY=<aleatória>
DEBUG=False
ALLOWED_HOSTS=richardson-barber-api-production.up.railway.app
ALLOWED_ORIGINS=["https://richardson-barber-front.vercel.app"]
PUBLIC_PORTAL_BASE_URL=https://richardson-barber-front.vercel.app
SECURE_SSL_REDIRECT=True
```

`token_blacklist` exige migração — o `preDeployCommand` da Railway já roda
`migrate`, então o deploy aplica `0016_*` + `token_blacklist`.

**Comportamento da SECRET_KEY no build/deploy:**
- `collectstatic` (fase de build, sem envs de runtime) tolera a ausência de
  `SECRET_KEY` usando uma chave efêmera — o build **não quebra**.
- `migrate` (preDeploy) e o `gunicorn` (runtime) **exigem** `SECRET_KEY` real e
  abortam com mensagem clara se ela não estiver definida.
- Ou seja: enquanto `SECRET_KEY` não for configurada na Railway, o build passa
  mas o deploy falha no `migrate`. Configure a variável para concluir o deploy.

## 4. Evolution API

`docker-compose.yml` agora publica a porta só em `127.0.0.1`. Se precisar de
acesso externo, coloque atrás de um proxy reverso com TLS e autenticação.
