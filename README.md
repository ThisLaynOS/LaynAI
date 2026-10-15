# LaynAI — Discord + Gemini 4 Argon

Assistente em português para Discord, com **`google/gemini-4-argon`** no Google Cloud, conversas com memória, pesquisa no Google, geração de imagens, vídeos e músicas e ferramentas de organização do servidor. Python 3.12+, SQLite e três dependências principais: `cryptography`, `websockets` e `Pillow`.

## Instalar e executar

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
cp .env.example .env
# Preencha as credenciais e o projeto no .env.
python scripts/doctor.py
python -m unittest discover -s test -v
python scripts/register.py
python src/main.py
```

Se você já tem `.env`, **preserve o arquivo existente**. No Windows, ative com `.venv\Scripts\activate` e use `Copy-Item` em vez de `cp`.

Nesta instalação OpenRC, o painel fica em **https://192.168.1.42:8787** (ou no IP IPv4 atual da máquina): status do bot, uso, backups, logs e botões de ligar/desligar/reiniciar. Ele roda em serviço separado (`laynai-dashboard`), escuta em todas as interfaces IPv4 e exige senha por HTTPS. A configuração e os cuidados de segurança estão em [docs/atualizacao.md](docs/atualizacao.md#painel-na-rede).

O modelo de texto usa obrigatoriamente estas configurações:

```dotenv
VERTEX_MODEL=google/gemini-4-argon
VERTEX_LOCATION=global
```

A rota é `https://aiplatform.googleapis.com/v1/projects/PROJETO/locations/global/endpoints/openapi/chat/completions`. O host **não** tem o prefixo `global-`. A integração do Argon usa `global` e também aceita `VERTEX_MODEL=gemini-4-argon`, acrescentando o prefixo `google/` exigido pelo endpoint compatível. Ao migrar uma instalação existente, atualize `VERTEX_MODEL` no `.env` e reinicie o serviço. A identidade de LaynAI informa Gemini 4 Argon.

## Comandos e conversas

| Recurso | Como usar | Visibilidade |
| --- | --- | --- |
| Conversa | `/perguntar texto:Explique este código` | Pública por padrão |
| Conversa privada | `/perguntar texto:Minha pergunta privado:true` | Só você |
| Menção | `@LaynAI sua pergunta` | Pública no canal |
| DM por texto | Envie uma mensagem diretamente ao bot | Na DM |
| Imagem | `/imagem texto:Uma cidade futurista formato:16:9` | Pública; aceita `privado:true` |
| Editar foto | `/imagem texto:Troque o fundo por uma praia foto:` + anexo | Pública; aceita `privado:true` |
| Editar avatar | `/imagem texto:Coloque óculos escuros e um fundo azul usuario:@pessoa` | Pública; aceita `privado:true` |
| Vídeo | `/video texto:Gato surfando segundos:6` | Pública; aceita `privado:true` |
| Música | `/lyra prompt:Pop animado em português sobre uma viagem com amigos` | Pública; aceita `privado:true` |
| Sua cor | `/cor hex:#FF5500` | Confirmação privada; cor visível no servidor |
| Canal/categoria | `/canal nome:geral tipo:text` | Confirmação privada; alteração no servidor |
| Cancelamento | `/cancelar` | Privada |
| Exportação | `/exportar` | Arquivo JSON privado com seus históricos deste canal |
| Limpar memória | `/limpar` | Privada |
| Uso e ajuda | `/status` e `/ajuda` | Privada |

É possível pedir uma pesquisa, imagens, vídeos e alterações do servidor em linguagem natural. Quando a pergunta depende de fato atual, o Gemini 4 Argon chama `web_search`. A busca usa Grounding with Google Search no Vertex (`gemini-2.5-flash` por padrão) e a resposta mostra os links e a consulta feita no Google. O modelo recebe somente ferramentas disponíveis e compatíveis com as permissões verificadas. Respostas longas incluem um arquivo `resposta.txt` completo; emojis são contabilizados conforme o tamanho usado pelo Discord.

Para editar uma foto, envie **a foto e a instrução na mesma mensagem**, na DM ou mencionando o bot no canal: `@LaynAI troque o fundo desta foto por uma praia`. Também funciona responder à mensagem que contém a foto (mencionando o bot no servidor), inclusive uma imagem que o bot acabou de gerar. O anexo da mensagem atual tem prioridade sobre o da resposta. `/imagem` e `/perguntar` aceitam a opção `foto`. Use uma foto **PNG, JPEG ou WebP de até 7 MB** por pedido. O Nano Banana recebe os bytes da foto original e devolve a imagem editada; sem `formato`, a edição não força uma proporção quadrada. Fotos enviadas sem texto recebem orientação para informar a edição. Para uma nova edição, anexe a foto ou responda diretamente à mensagem que a contém; os arquivos não ficam na memória da conversa. Em servidores, a leitura de anexos de mensagens anteriores de usuários pode exigir **Message Content Intent**; anexar foto e pedido juntos com uma menção evita depender dessa leitura.

Após atualizar, execute `python scripts/register.py` para disponibilizar as opções `foto` e `usuario` nos comandos correspondentes e reinicie o bot.

Para editar o avatar de alguém, use a opção **`usuario` do `/imagem`** e escreva as mudanças em **`texto`**. O bot recebe o usuário selecionado do Discord, baixa o avatar e envia a imagem ao Nano Banana. No servidor, usa o avatar específico daquele servidor quando disponível; caso contrário, usa o avatar global da conta ou o avatar padrão. Mesmo para avatares GIF, o bot pede primeiro a versão PNG estática do mesmo avatar ao Discord. Se ela falhar, tenta WebP estático do mesmo usuário/hash/servidor e o converte em PNG. Assim, a edição não depende de baixar a animação inteira. O conversor também reconhece GIF ou WebP animado pelo conteúdo e extrai o primeiro quadro, preservando a transparência; a edição resultante é uma imagem estática. A resposta identifica de quem é a versão editada e entrega o arquivo. Escolha `usuario` ou `foto`, um por pedido. A opção mantém os contextos de DM, servidores e instalação na conta, sem exigir que o bot consulte a lista de membros do servidor. Registre os comandos novamente após atualizar para disponibilizar `usuario`. As URLs seguem os [formatos oficiais de avatar do Discord](https://docs.discord.com/developers/reference#image-formatting).

A conversão usa Pillow: ao atualizar uma instalação existente, execute `python -m pip install -r requirements.lock` e reinicie o bot, ou reconstrua o serviço Docker com `docker compose up -d --build bot`. A conversão de avatares não exige novo registro dos comandos se `usuario` já aparece.

`/lyra` usa o **Lyria 3 Pro do Google** para transformar o `prompt` em um arquivo `musica.mp3`. Descreva gênero, instrumentos, clima e tema da letra, inclua uma letra própria ou peça uma música instrumental. O modelo aceita português e faixas de até 184 segundos; a duração pode ser orientada no prompt. Para trechos fixos de 30 segundos, configure `MUSIC_MODEL=lyria-3-clip-preview`. O comando usa a mesma autenticação Google das outras mídias, com a [API Interactions do Vertex](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/music/generate-music?hl=en) na região `global`; o projeto precisa ter acesso ao modelo. Veja os [modelos e limites do Lyria 3](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/lyria/lyria-3). Funciona nos contextos de servidor, DM e instalação de usuário, respeitando as mesmas permissões, quotas e listas de acesso. Aceita `privado:true` e `/cancelar`. O prompt e o áudio não entram no histórico de conversa. Registre os comandos após atualizar e reinicie o bot para ativar `/lyra`.

As embeds usam um visual discreto, com uma faixa lilás suave e o texto em destaque. Respostas e mídias não repetem nome, avatar, horário, título genérico ou rodapé do modelo; ajuda e status mantêm títulos curtos. Avisos usam linguagem de conversa, erros têm um tom rosado e o status usa verde suave. Vídeos aparecem com a legenda junto ao player. `/status` mostra uso e disponibilidade dos geradores configurados, sem consultar os provedores em tempo real. Reinicie o bot após atualizar para aplicar o visual.

Ao pedir código, o bot entrega um **arquivo para baixar** com nome e extensão, mantendo a explicação e os comandos de uso na mensagem. Projetos com vários arquivos ou pastas vêm em `projeto.zip`, com a estrutura original e um guia `LEIA-ME-LaynAI.md`. Funciona em `/perguntar`, menções e DMs, inclusive com `privado:true`. Experimente `/perguntar texto:Crie um script Python que organize arquivos por extensão` ou peça um site com HTML, CSS e JavaScript separados.

Comandos curtos de terminal e logs continuam visíveis para copiar. O código é preservado em UTF-8, sem execução no servidor, e fica no histórico da mesma conversa para pedidos como “corrija esse arquivo”. Se a geração for interrompida pelo limite do modelo, a resposta avisa; blocos não concluídos recebem a extensão `.partial` e projetos incompletos são identificados no nome do ZIP. Respostas extensas continuam disponíveis integralmente em um anexo de texto, com uma prévia que respeita parágrafos e blocos. As setas de fontes mantêm os arquivos anexados, seguindo a [edição de mensagens do Discord](https://docs.discord.com/developers/resources/message#edit-message).

O formato de saída usa cercas Markdown como `python filename=app.py`, uma por arquivo. O nome também pode vir em um título imediatamente anterior ao bloco; na ausência de nome, a extensão é inferida da linguagem. Nomes são normalizados, caminhos relativos são preservados no ZIP e colisões recebem sufixos. O protocolo de anexos é acrescentado também a `SYSTEM_PROMPT` personalizado; a personalidade continua configurável.

O histórico é isolado por **usuário, servidor, canal e visibilidade**. Perguntas privadas nunca alimentam respostas públicas. O bot não incorpora automaticamente o restante da conversa do canal. `/limpar` remove os dois históricos do usuário no canal, mas não mensagens já publicadas ou dados mantidos pelo provedor.

`/cancelar` interrompe a espera e libera o pedido local. Não desfaz ações concluídas nem garante o cancelamento da geração/cobrança no Google. Um pedido entrega no máximo uma imagem, um vídeo ou uma música; faça pedidos separados para mídias adicionais.

## Configurar o Discord

1. No [Developer Portal](https://discord.com/developers/applications), obtenha o Bot Token e o Application ID e preencha `DISCORD_BOT_TOKEN` e `DISCORD_APPLICATION_ID`.
2. Deixe **Interactions Endpoint URL vazio**: o projeto recebe interações pelo Gateway.
3. Instale com os escopos `bot` e `applications.commands` e permissões de ver/enviar mensagens, enviar em threads, ler histórico, anexar arquivos e inserir links/embeds.
4. Para `/cor`, conceda **Gerenciar Cargos** e coloque o cargo do bot acima dos cargos de cor. Para ferramentas de canais, conceda **Gerenciar Canais**. O bot não precisa de Administrador.
5. Membros que organizam canais precisam ser donos do servidor ou possuir **Administrador/Gerenciar Canais**. O nome de um cargo e a permissão Gerenciar Servidor, sozinhos, não concedem esse acesso.
6. Use `DISCORD_GUILD_ID` para registrar todos os comandos em um servidor de teste. Para comandos globais e DMs, deixe vazio. Execute `python scripts/register.py` após mudanças de comandos. O script atualiza os nomes deste projeto e preserva outros comandos; ele não remove registros antigos em outro escopo.

Para usar `/imagem`, `/perguntar` e `/video` também em **servidores de terceiros onde o bot não está instalado** e em DMs com outras pessoas, habilite **User Install** e **Guild Install** em **Developer Portal → Installation → Installation Contexts**. Em **Default Install Settings**, configure `applications.commands` para User Install; para Guild Install, use `applications.commands` e `bot`. Salve, registre os comandos globalmente e abra o link de instalação escolhendo **Adicionar aos meus apps**. Instalar somente no seu servidor não instala o app na sua conta. Esses são os [contextos de instalação documentados pelo Discord](https://docs.discord.com/developers/tutorials/developing-a-user-installable-app#choosing-supported-installation-contexts).

O comando se chama **`/imagem`**. Seu registro inclui servidor, DM com o bot e canais privados, mas alterar o código ou reiniciar o processo não atualiza os comandos no Discord: execute `python scripts/register.py` após a atualização, com `DISCORD_GUILD_ID` vazio para o registro global. As permissões do servidor também se aplicam: **Usar comandos de aplicativos** pode impedir o uso, e **Usar aplicativos externos** controla respostas públicas de apps instalados apenas na conta. [Permissões de aplicativos no Discord](https://support-apps.discord.com/hc/en-us/articles/26501864012951-Moderating-Apps-on-Discord).

O Gateway solicita `GUILDS`, `GUILD_MESSAGES` e `DIRECT_MESSAGES`. Menções e DMs funcionam sem o intent privilegiado de conteúdo. Para responder a mensagens do bot **sem mencioná-lo** em servidores, habilite **Message Content Intent** no portal e `MESSAGE_CONTENT_INTENT=true`. A leitura por REST não contorna a restrição de conteúdo. Allowlists também se aplicam a DMs; uma lista de servidores preenchida bloqueia DMs.

## Configurar o Google Cloud

Preencha `GOOGLE_CLOUD_PROJECT` com seu projeto. Habilite faturamento, Vertex AI (`aiplatform.googleapis.com`) e acesso ao modelo no Model Garden. A identidade precisa da permissão de previsão, normalmente com `roles/aiplatform.user`; o projeto de consumo pode exigir `serviceusage.services.use`.

Formas de autenticação suportadas:

- **ADC local:** execute `gcloud auth application-default login` e `gcloud auth application-default set-quota-project SEU_PROJETO`. Deixe `GOOGLE_APPLICATION_CREDENTIALS` vazio. O bot procura o arquivo em `CLOUDSDK_CONFIG` ou `~/.config/gcloud`.
- **Conta de serviço:** configure `GOOGLE_APPLICATION_CREDENTIALS` com um JSON fora do repositório, usando caminho absoluto.
- **VM Google Cloud:** use uma conta de serviço vinculada com escopo `cloud-platform` e deixe a variável de credencial vazia. O bot consulta o serviço de metadados.

Tokens são renovados e compartilhados entre pedidos concorrentes. Cancelar um pedido não cancela uma renovação utilizada por outros. Credenciais `external_account`/Workload Identity Federation ainda não são suportadas.

O cliente usa a [API compatível com Chat Completions do Google](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/openai). O Gemini recebe texto e a indicação de que há uma foto disponível para edição. As assinaturas opacas retornadas pelo modelo são preservadas entre as rodadas de ferramentas do mesmo pedido, sem aparecer nas respostas, logs ou histórico persistido. A imagem original é enviada ao Nano Banana pela ferramenta, seguindo a [API de edição de imagens do Google](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-edit-images). A disponibilidade de cada modelo de imagem/vídeo depende de acesso e região no projeto Google.

## Limites e opções

| Variável | Padrão | Efeito |
| --- | --- | --- |
| `MAX_CONCURRENT` | 3 | Pedidos em andamento; excedentes recebem aviso |
| `USER_COOLDOWN_SECONDS` | 10 | Intervalo entre pedidos do usuário em todos os canais |
| `USER_DAILY_REQUESTS` | 30 | Pedidos aceitos por usuário/dia UTC |
| `GLOBAL_DAILY_REQUESTS` | 200 | Pedidos aceitos pelo bot/dia UTC |
| `MAX_OUTPUT_TOKENS` | 8192 | Teto por chamada, permitindo códigos maiores; não força respostas longas |
| `MODEL_TEMPERATURE` | 0.5 | Variação para outros modelos; não enviada ao Argon |
| `MODEL_REASONING_EFFORT` | low | Raciocínio do Argon: `low`, `medium` ou `high` |
| `MODEL_TIMEOUT_SECONDS` | 90 | Prazo total de uma conversa, incluindo ferramentas |
| `MAX_TOOL_ROUNDS` | 6 | Máximo de chamadas sequenciais ao modelo por pedido |
| `MAX_TOOL_CALLS` | 16 | Máximo de ferramentas solicitadas por pedido |
| `VIDEO_TIMEOUT_SECONDS` | 300 | Prazo do comando `/video`; `.env.example` usa 180 |
| `MUSIC_TIMEOUT_SECONDS` | 300 | Prazo do comando `/lyra`, entre 30 e 600 segundos |
| `MUSIC_MODEL` | `lyria-3-pro-preview` | Música completa; aceita também `lyria-3-clip-preview` para 30 segundos |
| `MUSIC_LOCATION` | global | Região do Lyria 3; somente `global` |
| `HISTORY_PAIRS` | 8 | Pares de mensagens guardados por conversa |
| `MAX_CONTEXT_CHARS` | 64000 | Orçamento aproximado do contexto, incluindo código de respostas anteriores |
| `HISTORY_TTL_HOURS` | 24 | Expiração do histórico após inatividade |
| `ENABLE_IMAGES` / `ENABLE_VIDEOS` | true | Ativam os respectivos geradores |
| `ENABLE_MUSIC` | true | Ativa `/lyra`; desativado não consome quota ao receber o comando |
| `ENABLE_SEARCH` | true | Ativa a pesquisa Google usada pelo Gemini |
| `SEARCH_MODEL` | `gemini-2.5-flash` | Modelo Vertex com Grounding with Google Search |
| `SEARCH_LOCATION` | global | Região desse modelo |
| `ENABLE_SERVER_TOOLS` | true | Ativa ferramentas administrativas |
| `MESSAGE_CONTENT_INTENT` | false | Leitura de respostas sem menção; exige ativação no portal |
| `ALLOWED_GUILD_IDS` / `ALLOWED_USER_IDS` / `ALLOWED_CHANNEL_IDS` | vazio | IDs permitidos, separados por vírgula |
| `SYSTEM_PROMPT` | LaynAI doce e acolhedora | Substitui a personalidade; o protocolo de anexos é acrescentado; não concede permissões |

Seu `.env` existente pode manter limites diferentes. São aceitos no máximo 12 ajustes de canais por pedido. Alterações são serializadas por servidor dentro do processo. Ferramentas repetidas com os mesmos argumentos reutilizam o resultado de sucesso daquele pedido. IDs de canal/categoria são conferidos contra o servidor atual.

Os tetos de saída e contexto maiores permitem arquivos e revisões mais completos e podem aumentar o consumo de tokens em pedidos extensos. O histórico mais antigo é removido do contexto por turnos inteiros quando não cabe nesse orçamento; um arquivo excepcionalmente grande pode precisar ser dividido em partes.

A personalidade padrão está em `DEFAULT_SYSTEM`, no arquivo `src/config.py`: doçura inspirada na Cat Valentine, com um jeito meigo, animado, curioso e brincalhão, mantendo a identidade da LaynAI. Usa linguagem informal, diminutivos moderados e emojis ocasionais. O humor e a distração brincalhona ficam nas conversas leves; dúvidas técnicas continuam recebendo respostas claras e cuidadosas. Evita frases prontas de atendimento, intimidade forçada e respostas longas em conversas simples. Perguntas como “tudo bem?” recebem uma saudação calorosa, sem explicações sobre IA ou sentimentos. Em assuntos sérios, mantém cuidado e precisão; se perguntarem explicitamente sobre sua identidade, é honesta sobre ser uma assistente virtual. Sem `SYSTEM_PROMPT` no ambiente, essa personalidade é carregada automaticamente. Reinicie o serviço após alterá-la; `/limpar` é opcional para começar uma conversa sem o histórico do estilo anterior.

Pedidos aceitos continuam contando em caso de falha ou cancelamento. O consumo de tokens de texto soma todas as rodadas retornadas pelo modelo, mesmo quando a entrega final falha, e aparece em `/status`. Não inclui mídia, chamadas sem resposta de uso nem o custo exato de faturamento. Essas quotas não são um teto financeiro; configure limites e alertas também no Google Cloud.

O transporte respeita `Retry-After`, faz espera progressiva e cancela operações após o prazo. POSTs não são repetidos em erros ambíguos de rede/5xx para evitar geração ou criação duplicada; 429 pode ser repetido. A geração de música faz apenas uma tentativa, inclusive em 429. Leituras e operações idempotentes podem repetir 502/503/504. Falhas 400/401/403 não são repetidas. Uma resposta 400 do modelo nunca desativa ferramentas silenciosamente.

## Segurança e dados

- Chamadas de ferramenta só vêm do campo estruturado da API. Exemplos de JSON/XML no texto não executam ações. Não existe fallback que cria canais ou gera mídia apenas por encontrar palavras na pergunta.
- Tipos, campos obrigatórios, enums, campos extras e tamanhos de lotes são validados antes da execução. Permissões são verificadas no código.
- Cores usam cargos sem permissões. O bot atribui/remove esses cargos apenas no próprio solicitante e não edita um cargo compartilhado para recolorir outras pessoas.
- Mensagens geradas não notificam `@everyone`, cargos ou usuários. Ajuda, status, erros imediatos e exportações são privados.
- Logs JSON não incluem mensagens, credenciais, argumentos das ferramentas ou corpos de erro externos.
- Fotos para edição são baixadas apenas dos hosts de anexos do Discord; avatares usam somente as rotas de avatar do CDN, montadas a partir do usuário selecionado no comando. Os downloads não enviam credenciais nem seguem redirecionamentos, e respeitam o limite de tamanho e a verificação do formato. Os bytes e URLs assinados não são gravados no histórico SQLite nem enviados ao modelo de texto; os bytes são enviados ao Google apenas quando a ferramenta de imagem é executada.
- O download das fotos usa o mesmo `User-Agent` identificado como `DiscordBot` da API. O identificador padrão `Python-urllib` pode ser recusado pelo CDN com HTTP 403; veja a [orientação do Discord sobre User-Agent](https://docs.discord.com/developers/reference#user-agent).
- Histórico textual fica no SQLite, com permissão local restrita e sem criptografia própria. Proteja o disco e os backups. Limpeza ocorre a cada minuto; contagens de tokens são retidas por 30 dias.
- O projeto não oferece exclusão de canais, atribuição de permissões, execução de código, navegador aberto, voz ou leitura de documentos anexados. Fotos são aceitas para edição com Nano Banana. A pesquisa é uma consulta ao Google, com fontes devolvidas na resposta.

## Atualizações automáticas nesta máquina

O serviço OpenRC `laynai` usa este diretório. Depois que um pull request aprovado entra em `main`, o atualizador local testa o commit e reinicia o serviço. Veja [Atualização automática pelo Git](docs/atualizacao.md).

## Operação com Docker

```sh
docker compose up -d --build
docker compose exec bot python scripts/register.py
docker compose logs -f --tail=100 bot
```

Uma réplica/processo/shard, volume local `bot-data`, usuário sem privilégios, filesystem somente leitura, limites de recursos e rotação de logs. Não rode outra instância do mesmo bot simultaneamente. `/healthz` verifica o processo; `/readyz` verifica também Gateway e acesso ao SQLite. Esses endpoints **não** comprovam o acesso ao modelo. A porta do Compose fica exposta apenas em `127.0.0.1`.

Para montar credenciais do host, defina `GOOGLE_CREDENTIALS_HOST_PATH` com o caminho absoluto e use:

```sh
docker compose -f compose.yaml -f compose.credentials.yaml up -d --build
```

O arquivo é montado como secret em `/run/secrets/google_credentials`. A identidade da VM também pode ser usada. SQLite exige disco persistente; Cloud Run com disco efêmero e múltiplas réplicas requer outra arquitetura de armazenamento e coordenação.

## Diagnóstico e backup

```sh
# Sem chamadas externas; não mostra segredos.
python scripts/doctor.py
# Consulta a identidade no Discord e faz uma pequena geração paga no Google.
# Não publica mensagens nem altera canais.
python scripts/doctor.py --live
# Snapshot consistente via API de backup do SQLite, incluindo transações no WAL.
python scripts/backup.py
# Caminho explícito; um arquivo existente nunca é sobrescrito.
python scripts/backup.py --output backups/antes-da-atualizacao.sqlite
```

Em Docker, grave o backup em `/app/data/backups/` e copie-o para um armazenamento protegido fora da instância. Para restaurar, pare o bot, preserve o diretório de dados atual e restaure o snapshot em um diretório limpo com as permissões adequadas. Não combine um snapshot com arquivos WAL/SHM de outra versão. Nunca use `docker compose down -v` se quiser preservar os dados.

| Problema | Verificação |
| --- | --- |
| Bot offline | Token, rede de saída HTTPS/WSS e logs `gateway_*` |
| Comando ausente | Registro, servidor de teste versus global, instalação e propagação |
| Resposta sem menção não funciona | Message Content Intent no portal e na configuração |
| Erro 401/403 no Google | Credenciais, IAM, API, faturamento e acesso ao modelo |
| Erro 404 no Google | Modelo exato e `VERTEX_LOCATION=global` |
| Erro 429 | Quota/capacidade do provedor ou do Discord |
| Mídia indisponível | Feature flag, modelo, acesso e permissões de anexos |
| Avatar não editado | A resposta distingue download/conversão, recusa ou ausência de imagem, acesso ao Google e prazo excedido. Logs `image_failed` têm código e status; `action_failed` inclui comando e etapa, sem registrar a foto, prompt ou credenciais |
| Cor não aparece | Hierarquia dos cargos; cargos superiores podem definir outra cor |
| DNS/conexão falha | Resolução de nomes, firewall e conectividade do host |

## Validação e manutenção

```sh
python -m pip check
python -m compileall -q src scripts
python -m unittest discover -s test -v
```

A suíte cobre migração de endpoint, contratos de autenticação/API, Gateway, quotas, memória, privacidade, ferramentas, permissões, cancelamento, prazos, Unicode e backup. A edição de fotos cobre anexos de comandos, menções, DMs e respostas, envio dos bytes originais ao Nano Banana, entrega da imagem editada, arquivos inválidos, tamanho e cancelamento. A edição de avatares cobre o usuário selecionado, avatar global, avatar de servidor, avatares padrão, conflito com anexo e falhas de download. GIFs e WebPs animados reais verificam a conversão do primeiro quadro em PNG, a transparência, o conteúdo enviado ao Nano Banana e os limites de resolução e tamanho; avatares corrompidos são rejeitados antes de chamar o modelo. Chamadas aos provedores são simuladas nos testes; a assinatura RSA usa criptografia real com execução do trabalho em thread substituída por execução local no teste. O workflow `.github/workflows/ci.yml` executa verificações em Python 3.12 e 3.14. Dependências estão fixadas em `requirements.lock`.

Os testes de música verificam o contrato da API Lyria, a decodificação e entrega do MP3 nos três contextos do Discord, privacidade, quota, cancelamento, prazo, falhas de autenticação/geração/entrega e arquivos inválidos ou grandes demais. A API Google e o envio do áudio no Discord são simulados nesses testes.

Os testes de entrega de código cobrem conteúdo UTF-8 e indentação, arquivos vazios, nomes e extensões, cercas Markdown aninhadas, ZIPs com pastas, colisões de nomes, caminhos seguros, geração parcial, comandos curtos, histórico para correções, fontes, respostas privadas e envio multipart. Nesta revisão, os 145 testes, a compilação, `pip check` e o diagnóstico local passaram. Uma geração real com Gemini 4 Argon retornou HTML, CSS e JavaScript separados, transformados em um ZIP válido com guia de uso; o upload foi verificado localmente, sem publicar mensagens no Discord. Para ativar no processo hospedado, atualize os arquivos e reinicie o bot. O `.env` local usa 8192 tokens de saída e 64000 caracteres de contexto.

Na revisão de música, os 114 testes passaram em Python 3.14.7, assim como a compilação, `pip check` e o diagnóstico local. A execução local usou um temporizador no loop do runner para contornar uma limitação do sandbox no encerramento de threads; a conversão das imagens em thread foi real. `/lyra` foi registrado globalmente com `prompt` e `privado`; uma consulta posterior confirmou os três contextos e os dois tipos de instalação, preservando todos os comandos existentes. `/imagem` também está registrado globalmente com a opção `usuario`.

Na correção de avatares animados, os 120 testes e a compilação passaram. O CDN entregou um avatar real de 75 quadros/3,49 MB como PNG estático de 240 KB, ambos com HTTP 200. Uma chamada real ao Nano Banana com um GIF de teste convertido em PNG retornou uma imagem PNG editada de 1024×1024. Esse teste confirma a integração, mas não reproduz um perfil/prompt específico sem os dados do pedido que falhou. Não houve geração de música ao vivo nem implantação do código. Atualize o código no host e reinicie uma única instância; o diagnóstico remoto está disponível em `doctor.py --live` (verifica Discord e texto, não os geradores de mídia).

Na migração para Gemini 4 Argon, os 122 testes, a compilação e o diagnóstico local passaram. Chamadas reais ao endpoint global com `google/gemini-4-argon` retornaram HTTP 200 para texto, chamada de ferramenta e continuação com o resultado de teste da ferramenta, preservando a assinatura retornada pelo modelo. O `.env` local e a descrição global de `/perguntar` foram atualizados. A troca do processo hospedado exige atualizar os arquivos e reiniciar o bot; não houve implantação remota nesta revisão.

Referências: [Gemini no endpoint compatível](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/openai), [Discord Gateway](https://docs.discord.com/developers/events/gateway), [comandos Discord](https://docs.discord.com/developers/interactions/application-commands), [ADC](https://docs.cloud.google.com/docs/authentication/application-default-credentials).
