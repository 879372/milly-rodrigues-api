# milly-rodrigues-api

API Django/DRF do sistema Milly Rodrigues, preparada para hospedagem na Railway.

## Implantação na Railway

1. Conecte este repositório a um novo projeto Railway.
2. Adicione um banco PostgreSQL.
3. Configure as variáveis descritas em `.env.example`.
4. Defina `ALLOWED_ORIGINS`, `BOOKING_URL` e `PUBLIC_PORTAL_BASE_URL` com o domínio do frontend na Vercel.
5. Gere uma `SECRET_KEY` exclusiva para produção.

O comando de inicialização e a configuração de build já estão definidos em `Procfile`, `railway.toml` e `nixpacks.toml`.
