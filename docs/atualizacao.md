# Atualização automática pelo Git

Nesta máquina, o bot roda em `/root/chatbot` como serviço OpenRC `laynai`. O `crond` chama `scripts/update_from_git.py` a cada cinco minutos. O atualizador busca somente `origin/main`; portanto, uma alteração do seu amigo entra em produção depois que você aceita o pull request e faz o merge em `main`.

## Fluxo de revisão

Peça ao colaborador para abrir um pull request. Revise o código e aguarde o workflow `Tests` passar antes do merge. No GitHub, proteja a branch `main` para exigir pull request e aprovação, bloquear force push e exigir os status checks do workflow. O código aprovado tem acesso às credenciais locais quando o bot é reiniciado, mesmo que os segredos não estejam no repositório.

O `.env`, as credenciais Google, a chave SSH e o SQLite ficam fora do Git. Antes de um novo commit, confira `git status --short` e `git diff --cached --name-only`. Se um segredo já tiver entrado no histórico, revogue ou troque a credencial antes de publicar.

## O que o atualizador faz

1. Recusa alterações locais em arquivos versionados e histórico remoto divergente.
2. Testa a revisão nova em uma área temporária, sem alterar os arquivos usados pelo serviço.
3. Para o serviço e cria, **antes de aplicar o commit**, um ponto de restauração privado em `backups/restorepoints/`: `code.tar` (todos os arquivos versionados da revisão anterior), `.env`, `bot.sqlite` consistente e o SHA anterior. Se a cópia falhar, não muda o código e religa a versão anterior.
4. Avança a branch local, inicia `rc-service laynai` e confirma `/readyz` e a supervisão pelo OpenRC.
5. Se a implantação falhar, para o serviço, volta o código à revisão anterior, restaura `.env` e SQLite do ponto de restauração, e inicia a versão antiga. Não tenta novamente a revisão ruim até aparecer outro commit remoto. Se o rollback também falhar, o ponto de restauração fica preservado para recuperação manual.

Alterações em `requirements.lock` exigem atualização manual do `.venv`; o atualizador interrompe a implantação sem reiniciar o bot. Mudanças nos comandos de barra do Discord também podem exigir `python scripts/register.py` após a implantação.

Os pontos de restauração ficam **só nesta máquina**, fora do Git, com permissões restritas. Eles cobrem o código versionado, `.env` e o banco configurado em `DATABASE_PATH`; não cobrem arquivos externos, como credenciais Google fora do projeto, nem mudanças manuais no `.venv` (que o atualizador não altera). Não há limpeza automática dos pontos de restauração. Se uma versão maliciosa for aprovada, ela pode acessar os segredos locais: backup não substitui revisão de PR.

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

## Painel na rede

O painel roda como serviço OpenRC separado (`laynai-dashboard`) em **http://192.168.1.42:8787** nesta máquina. O servidor escuta em `0.0.0.0:8787` (todas as interfaces **IPv4**), mas só aceita clientes de loopback e redes IPv4 privadas (`10/8`, `172.16/12`, `192.168/16`). Exige autenticação Basic para páginas, dados e comandos. O usuário é `admin`; a senha aleatória fica somente em `data/.dashboard-password`, fora do Git, com permissão `0600`. Para vê-la no terminal da máquina:

```sh
sed -n '1p' /root/chatbot/data/.dashboard-password
```

O painel mostra saúde do bot, uso diário, revisão instalada, pontos de restauração e últimos logs de bot, erros e deploys. Os botões usam apenas as ações fixas `start`, `stop` e `restart` do serviço `laynai`, sem executar texto digitado no navegador. Desligar pelo painel cria `data/.dashboard-paused`, pausa o cron e remove `laynai` do runlevel padrão para ele continuar desligado após reboot. Ligar ou reiniciar com sucesso reativa o início automático e remove a pausa. Quando o painel está rodando, o atualizador também o reinicia após cada deploy para carregar alterações do próprio painel.

```sh
rc-service laynai-dashboard status
tail -n 100 /var/log/laynai/dashboard.err
```

O navegador não recebe credenciais do bot, conteúdo dos backups nem comandos de shell. As ações POST exigem origem da mesma página e token anti-CSRF, além da senha. **HTTP não criptografa a senha nem os dados:** use somente numa rede local confiável; não encaminhe a porta no roteador nem exponha o painel diretamente à internet. Se precisar de acesso remoto, use uma VPN ou um proxy HTTPS confiável. O arquivo OpenRC versionado está em `ops/laynai-dashboard.openrc`.

Referências: [proteção de branches no GitHub](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches) e [supervise-daemon no OpenRC](https://github.com/OpenRC/openrc/blob/master/supervise-daemon-guide.md).
