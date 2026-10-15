# Atualização automática pelo Git

Nesta máquina, o bot roda em `/root/chatbot` como serviço OpenRC `laynai`. O `crond` chama `scripts/update_from_git.py` a cada cinco minutos. O atualizador busca somente `origin/main`; portanto, uma alteração do seu amigo entra em produção depois que você aceita o pull request e faz o merge em `main`.

## Fluxo de revisão

Peça ao colaborador para abrir um pull request. Revise o código e aguarde o workflow `Tests` passar antes do merge. No GitHub, proteja a branch `main` para exigir pull request e aprovação, bloquear force push e exigir os status checks do workflow. O código aprovado tem acesso às credenciais locais quando o bot é reiniciado, mesmo que os segredos não estejam no repositório.

O `.env`, as credenciais Google, a chave SSH e o SQLite ficam fora do Git. Antes de um novo commit, confira `git status --short` e `git diff --cached --name-only`. Se um segredo já tiver entrado no histórico, revogue ou troque a credencial antes de publicar.

## O que o atualizador faz

1. Recusa alterações locais em arquivos versionados e histórico remoto divergente.
2. Testa a revisão nova em uma área temporária, sem alterar os arquivos usados pelo serviço.
3. Cria um snapshot consistente do SQLite em `backups/`.
4. Avança a branch local, reinicia `rc-service laynai` e confirma `/readyz` e a supervisão pelo OpenRC.
5. Se o reinício falhar, restaura a revisão anterior e não tenta a revisão ruim novamente até aparecer outro commit remoto.

Alterações em `requirements.lock` exigem atualização manual do `.venv`; o atualizador interrompe a implantação sem reiniciar o bot. Mudanças nos comandos de barra do Discord também podem exigir `python scripts/register.py` após a implantação.

O agendamento nesta máquina usa o crontab do root:

```cron
*/5 * * * * /root/chatbot/.venv/bin/python /root/chatbot/scripts/update_from_git.py >> /var/log/laynai/update.log 2>&1
```

Para consultar imediatamente ou acompanhar o serviço:

```sh
/root/chatbot/.venv/bin/python /root/chatbot/scripts/update_from_git.py
rc-service laynai status
tail -n 100 /var/log/laynai/update.log
```

O atualizador registra a revisão implantada em `data/.deployed-revision` e a revisão que falhou em `data/.failed-revision`. Ambos ficam fora do Git. O serviço OpenRC continua supervisionando o processo entre atualizações.

Referências: [proteção de branches no GitHub](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches) e [supervise-daemon no OpenRC](https://github.com/OpenRC/openrc/blob/master/supervise-daemon-guide.md).
