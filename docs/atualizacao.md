# Atualização pelo repositório

O bot recebe código da branch `main` após você aceitar um pull request. O processo que consulta o Git roda **no computador host**, fora do container. O bot não precisa de acesso ao GitHub nem de permissão para alterar o próprio código.

## 1. Publicar o código

Se o diretório ainda não for um repositório Git, inicialize-o. Prepare o primeiro commit com uma lista explícita de arquivos:

```sh
git init -b main
git add .gitignore .dockerignore .env.example .github Dockerfile README.md compose.yaml compose.credentials.yaml requirements.txt requirements.lock docs scripts src test
git diff --cached --name-only
git diff --cached --check
git commit -m "Publicar código do bot"
```

Confira a lista **antes** do commit: `.env`, `data/`, `backups/`, chaves privadas e arquivos de credenciais não devem aparecer. O `.gitignore` protege nomes conhecidos, mas não substitui essa conferência. Se qualquer segredo já tiver entrado em um commit, removê-lo em outro commit não apaga o histórico; revogue ou troque a credencial antes de publicar.

Crie um repositório vazio no serviço escolhido e conecte o endereço fornecido por ele:

```sh
git remote add origin ENDERECO_DO_REPOSITORIO
git push -u origin main
```

Guarde o token Discord no `.env` local e o JSON Google fora do repositório. O SQLite continua no volume Docker `bot-data`. Se você já tem um bot rodando nesta máquina, atualize a **mesma instalação e o mesmo projeto Compose** para não iniciar uma segunda instância com o mesmo token.

## 2. Aprovar alterações

Seu amigo cria uma branch e abre um pull request. Você revisa a mudança e só faz o merge em `main` depois que a verificação `Tests` passar. No GitHub, configure uma regra para `main` que exija pull request, sua aprovação e as verificações do workflow, além de impedir force push. A disponibilidade de proteção obrigatória em repositórios privados depende do plano; se não estiver disponível, mantenha o merge sob seu controle e não conceda escrita direta em `main`.

Mesmo sem credenciais no Git, código novo executado pelo bot pode ler as credenciais locais. Revise alterações em `src/`, `scripts/`, `Dockerfile`, arquivos Compose e workflows com esse acesso em mente.

## 3. Atualizar no computador de casa

O atualizador requer Linux, Git, Docker Compose e um Python com as dependências do projeto instaladas. Use a instalação existente do bot; deixe `.env` no diretório do projeto. Para um repositório privado no GitHub, configure uma *deploy key* de leitura no host. O atualizador só precisa buscar o código.

Execute uma vez para conferir:

```sh
.venv/bin/python scripts/update_from_git.py
```

Se você monta um JSON de conta de serviço com `compose.credentials.yaml`, mantenha `GOOGLE_CREDENTIALS_HOST_PATH` definido no ambiente ou no `.env` local e use:

```sh
.venv/bin/python scripts/update_from_git.py --with-google-credentials-file
```

Para consultar automaticamente a cada cinco minutos, adicione ao `crontab -e`, substituindo `/CAMINHO/laynai` pelo caminho absoluto da instalação:

```cron
*/5 * * * * /CAMINHO/laynai/.venv/bin/python /CAMINHO/laynai/scripts/update_from_git.py >> /CAMINHO/laynai/data/update.log 2>&1
```

Acrescente `--with-google-credentials-file` à linha do cron quando usar o arquivo de credenciais. O cron precisa rodar com o mesmo usuário que consegue executar `docker compose` e ler a credencial Google. Não execute dois bots simultaneamente.

O script usa um bloqueio para impedir atualizações sobrepostas. Ele busca `origin/main`, aceita apenas avanço normal do histórico, testa o código, valida o Compose, constrói a imagem e espera `/readyz`. Se a nova versão não ficar pronta, tenta voltar à imagem anterior. Uma falha fica registrada em `data/update.log` e a versão não é marcada como implantada; a próxima execução tenta novamente. Se você alterar a configuração Compose de forma incompatível, pode ser necessária uma restauração manual.

Referências: [proteção de branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches), [chaves de implantação](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys) e [recriação de containers pelo Compose](https://docs.docker.com/reference/cli/docker/compose/up/).
