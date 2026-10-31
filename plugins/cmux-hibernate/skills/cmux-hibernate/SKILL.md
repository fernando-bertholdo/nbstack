---
name: cmux-hibernate
description: Use quando o usuário quiser fechar o cmux para liberar memória ou reiniciar a máquina sem perder as sessões do Claude Code abertas, e quando quiser conferir ou restaurar o ambiente depois de reabrir. Cobre "vou fechar o cmux", "a máquina está travando", "será que voltou tudo?", "reabra minhas sessões".
---

# cmux-hibernate

Põe para dormir as sessões Claude Code espalhadas pelos workspaces do cmux: encerra o
processo `claude` de cada aba ociosa, o que libera a memória na hora e tira a aba da
restauração automática do cmux, e confere isso no arquivo que o cmux lê ao reabrir.
Depois de reabrir, confere aba a aba e retoma o que o usuário pedir.

## Antes de fechar o cmux

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hibernate.py"          # dry-run: o plano
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hibernate.py" --apply  # encerra e confere
```

Rode o dry-run primeiro e mostre o plano ao usuário. O dry-run não encerra nada nem
escreve no cmux; ele grava só o inventário em `ultimo-dry-run/`, fora da retenção. O
`--apply` **encerra processos** — só use depois que o usuário confirmar.

O plano traz uma ação por aba:

- **encerrar**: processo vivo e ocioso; recebe SIGTERM, e a memória sai na hora;
- **limpar**: armada no cmux sem processo (um restore que falhou); o registro é limpo;
- **dormindo**: já sem processo e desarmada;
- **pular**: ocupada, sessão em mais de uma aba, ou a própria sessão que roda o comando;
- **controle**: a aba de onde o comando roda, que continua armada.

"Ocupada" é o transcript escrito há menos de 60 s, ou o registro do hook do cmux dizendo
que há turno, pedido de permissão ou trabalho de fundo (subagente) pendente. Uma aba
pulada não dorme: diga qual é e por quê, e rode de novo quando o turno dela acabar.

Só diga ao usuário que pode dar Cmd+Q quando a saída do `--apply` disser "Pode dar
Cmd+Q". Ela só diz isso quando todas as abas-alvo foram conferidas desarmadas no autosave
do cmux. Se a saída listar alguma como FALHOU (código de saída 1), não libere o
fechamento: aquela aba pode subir sozinha ao reabrir.

Repita ao usuário as duas instruções que a saída traz:

- não abrir as abas encerradas antes do Cmd+Q, porque abrir uma rearma a aba;
- depois do Cmd+Q, esperar uns 10 s antes de reabrir o cmux. Se ele reabrir antes de os
  processos antigos terminarem, o restore da aba de controle pode falhar com
  "conflict: The surface restore record changed".

A aba de controle é a única que sobe quando o cmux reabre. `--all` a inclui; se o
comando roda dentro dela, ela é pulada e o usuário precisa sair dela com `/exit` antes
do Cmd+Q.

Para uma aba só, `--surface <uuid|surface:N>`: planeja aquela aba e nenhuma outra,
inclusive a de onde o comando roda.

## Depois de reabrir

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wake.py"              # confere
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wake.py" --up <ws>    # retoma um workspace
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wake.py" --up all     # retoma todas
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wake.py" --rebuild    # recria o que sumiu
```

Quando o usuário disser que reabriu o cmux, ou perguntar se voltou tudo, rode a
conferência antes de responder. Ela compara o plano do último `--apply` com o estado
real. Rodada antes do Cmd+Q, ela avisa que a conferência que vale é a de depois.

Uma aba encerrada não volta com o Claude sozinha. Para retomar, use `wake.py --up` ou o
comando do `INVENTARIO.md` do snapshot. O que a aba mostra ao ser aberta (shell vazio) e
se o clique nela inicia o Claude são **a confirmar na primeira reabertura**: até
31/10/2026 isso não tinha sido medido com o processo encerrado antes do Cmd+Q.

## Ao interpretar a saída do wake

- **dormindo** é o estado saudável. Não sugira `--up` por reflexo, só se o usuário pedir.
- **subiu sozinha** é falha: a aba devia dormir e o cmux a lançou ao reabrir. Diga quais
  são e ofereça rodar o `hibernate.py` de novo, que encerra as ociosas.
- **não dormiu** é falha: o processo é anterior ao hibernate, o encerramento não pegou.
- **acordada por você às HH:MM BRT**: alguém abriu a aba depois de reabrir. Nada se
  perdeu, mas ela gasta memória e ficou armada para o próximo fechamento. Ofereça pô-la
  para dormir de novo com `hibernate.py --surface <aba> --apply`.
- **armada**: sem processo, mas registrada para subir no próximo fechamento. O próximo
  `hibernate.py --apply` a limpa.
- **ausentes**: a aba não existe mais na estrutura. É o caso de `--rebuild`.
- **sem transcript** não é recuperável: o histórico não existe mais em disco. Diga isso
  com todas as letras em vez de sugerir tentativas.
- **anomalia: sessão em mais de uma aba** é pulada de propósito. Reporte, não contorne.

"Nada a corrigir" só aparece quando não há subiu sozinha, não dormiu, armada nem
ausente. Com qualquer um deles, não diga ao usuário que voltou tudo.

## O que não fazer

- Não use `cmux surface resume set` para desarmar: no cmux 0.65.2 ele é recusado em
  silêncio e responde `OK`. Foi o que fez 11 abas subirem sozinhas em 31/10/2026.
- Não mande SIGKILL num processo que não saiu com o SIGTERM. Reporte e peça `/exit`.
- Não invente um "registro de sessões" paralelo nem tente conciliar snapshots antigos.
  Só os 5 últimos `--apply` ficam guardados. Se o usuário pedir histórico, o que existe
  são os transcripts em `~/.claude/projects/`.
