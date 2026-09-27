Amigos, olá.

Andei pensando um pouco, e acho que não vai me matar se, mais ou menos a cada duas semanas, a página do Bernstein no LinkedIn mencionar, pessoalmente, cada colaborador que quiser ser mencionado, e agradecer a todos em uma única publicação conjunta.

Uma publicação. Não uma por pessoa. Nossos seguidores não precisam de dez publicações em fila, e eu não preciso de um segundo emprego escrevendo isso. Mas dentro dessa publicação única quero tentar dizer, por pessoa, o que você realmente fez, para que, se um possível empregador ler, entenda o que você construiu em um projeto real, e para que você não fique constrangido em compartilhar.

Aliás, já começamos isso. A #5524 tem a página do LinkedIn e várias pessoas dizendo "pode me mencionar", e a #6229 foi a primeira tentativa de um agradecimento consolidado. Então isso não é nada novo. É eu tentando parar de montar isso na mão todas as vezes. Quem já disse sim na #5524 não precisa dizer de novo; eu já tenho vocês anotados.

## Qual é a proposta

O fluxo todo, do seu lado:

1. Você fez um trabalho relevante no Bernstein nos últimos três meses.
2. Você segue a página do projeto: https://www.linkedin.com/company/bernstein-run/
3. De vez em quando a página publica uma publicação consolidada nomeando quem optou por participar e o que fez.
4. Você compartilha de novo se quiser, idealmente marcando o Bernstein.
5. O projeto ganha um alcance que não teria de outra forma. Você ganha um registro público e profissional que não foi escrito por você mesmo sobre você mesmo.

É isso. Não tem passo seis.

Se você está procurando emprego agora, existe uma alternativa: posso escrever uma recomendação no LinkedIn, do meu perfil (https://www.linkedin.com/in/alex-chernysh/), em vez disso. Não é trabalho nenhum. Mas quero ser direto sobre a troca: a recomendação fica no meu perfil e ajuda você; o compartilhamento da publicação conjunta ajuda você e o projeto ao mesmo tempo. E, sinceramente, quase ninguém rola até a seção de Recomendações mesmo. Então use a recomendação principalmente se estiver procurando ativamente, e o compartilhamento nos outros casos. Se quiser as duas coisas, é só falar.

Sobre o valor de tudo isso. Se você realmente fez o trabalho, não escreve isso. Coloque o Bernstein no seu CV, no seu portfólio, onde for cabível. Eu faço isso, alguns de vocês já fazem, e acho que é um bom sinal: uma contribuição mesclada (merged) em um projeto open-source sério, que você consegue explicar linha por linha, vale mais do que uma linha de currículo dizendo "sabe Python". Essa é minha opinião, não um estudo. Também não é promessa de nada: isso é um projeto voluntário, ninguém está contratando, e prefiro dizer isso claramente agora do que deixar alguém interpretar diferente depois.

## Como dizer sim

Envie um e-mail. Endereço e assunto exatamente assim:

> **To:** forte@bernstein.run
> **Subject:** `BRNSTN-PR-LNKD`

No corpo, algumas linhas que um script consegue ler:

```
GITHUB=your-github-login
OPT_IN=YES
RECOMMENDATION=NO
```

Coloque `RECOMMENDATION=YES` se quiser a recomendação do LinkedIn em vez da publicação, ou além dela. Adicione uma linha `NOTE=` se houver algo que eu deva saber (por exemplo, que você quer ver o texto antes de sair; algumas pessoas pediram isso e está tudo bem). Para sair depois, mesmo endereço, mesmo assunto, `OPT_IN=NO`.

Por favor, não mude a linha de assunto. O parser precisa exatamente dessa string. O volume de e-mails que chega é tal que isso não é questão de gosto; uma mensagem com outro assunto cai na fila geral e não posso prometer que algum dia eu veja.

## O filtro suave, e por que ele é suave

Para decidir quem entra em uma dada publicação sem eu ter que ler cada PR na mão, existe um filtro mecânico: por volta de 1.000 linhas adicionadas em pull requests mesclados nos últimos 30 dias, com arquivos gerados excluídos. Não é um KPI. Não é uma métrica de qualidade. Não é um julgamento sobre ninguém. É um jeito barato de um script montar um grupo a cada duas semanas sem um humano no meio.

E não é uma cerca rígida. Aqui tem gente fazendo trabalho de segurança, revisões, documentação, benchmarks, plumbing de release e depuração que pode ser enormemente valioso com quarenta linhas de diff. Se você fez um trabalho relevante e por algum motivo não chegou a mil, escreva para o mesmo endereço com o mesmo assunto e diga isso. Não somos monstros; a gente resolve. A resposta usual é achar a próxima tarefa para você e você passar do infeliz milhar na contribuição seguinte.

Os nomes na lista periódica saem em ordem alfabética. Não por número de PRs, nem por quem eu gosto mais. Alfabética.

## GitHub Discussions e a página do LinkedIn não competem

Fazem trabalhos diferentes. Para notícia rápida (um release, uma mudança, uma atualização da comunidade), a página do LinkedIn é o canal mais rápido. GitHub Discussions é onde você participa do projeto de fato: vê o contexto, discute, propõe, encontra a resposta, e isso fica registrado onde a próxima pessoa consegue achar. Se você quer as duas coisas, a notícia e a profundidade, siga os dois. Decisões continuam acontecendo em issues e pull requests; isso não mudou.

## Por que estou fazendo isso, afinal

Muitos de vocês são jovens. Alguns nem tanto. Mas para quem está começando, um projeto open-source de verdade é uma chance bem rara de experimentar não só código, mas revisão, arquitetura, testes, documentação, segurança, releases, coordenação, até um pouco de escrita pública, e de tomar decisões que têm consequências. Se eu pudesse ter trabalhado em algo assim enquanto estava na faculdade, minha vida provavelmente teria sido bem diferente. Então quero apoiar quem está começando a construir o máximo que puder.

Meu modelo é simples: cortar marcos em pedaços que uma pessoa consegue segurar, dar a alguém uma tarefa real, deixar a decisão com essa pessoa sempre que puder ser dela, não tirar a tarefa depois do primeiro erro, e deixar as pessoas assumirem mais responsabilidade conforme avançam.

Para quem está estudando algo relacionado a computação agora, ou só começando: eu vejo vocês. Estou com vocês. Vocês estão indo bem.

Mais uma opinião, claramente marcada como tal. Saber programar está virando algo básico, como saber digitar. Saber pensar, ver o trade-off, tomar uma decisão que você consegue defender, isso ainda é raro, e na era da IA suspeito que fica mais raro e mais valioso, não menos. Este é um lugar para praticar exatamente isso.

## Como o projeto é conduzido, por ora

Até o fim deste ano calendário eu continuo sendo o único maintainer. Isso é deliberado. Não significa que o maintainer decide tudo. A propriedade é minha; não acho que eu deva escolher pessoalmente cada vírgula da arquitetura. Se você veio fazer um PR, quero que você pense, não que adivinhe o que o maintainer quer.

Quando eu tiver capacidade, e agora mesmo estou meio enterrado em outras coisas, quero tentar dar um pouco mais de forma ao trabalho, com "departamentos" virtuais, do jeito que uma empresa normal tem. Tudo voluntário, sem obrigações, sem burocracia por burocracia. Para começar, vejo só dois: P&D, que já vive em issues e pull requests e precisa de pouco de mim, e PR / outreach, que provavelmente vou supervisionar mais de perto, porque comunicação sem estrutura vira "devíamos fazer isso algum dia" mais rápido do que qualquer outra coisa que eu conheço.

Depois, se houver gente ativa suficiente, papéis podem aparecer: um diretor de desenvolvimento, um de operações, um de segurança, um de QA e docs. Não estou criando esses títulos com antecedência. O número de pessoas determina a estrutura, não o contrário. Ninguém precisa de uma corporação de seis pessoas.

## Dinheiro, porque alguém sempre pergunta

Quando alguém pergunta se existe uma oportunidade remunerada para trabalhar no Bernstein, às vezes eu dou uma risadinha nervosa. Bernstein não ganha dinheiro. Gasta o meu. Eu nem conto o meu tempo; só fico olhando o que sai da conta, num país bem caro, enquanto estou entre empregos e as circunstâncias provavelmente vão me dar pelo menos mais duas semanas nesse projeto. Então o que estou fazendo não é trabalho não remunerado. É trabalho no prejuízo. Eu gosto do trabalho. Isso é, basicamente, por que estamos todos aqui.

Gratuito para o usuário não significa gratuito para o maintainer. A infraestrutura por trás disso custa da ordem de uns dois mil dólares por mês, e essa conta chega quer alguém publique algo no LinkedIn ou não.

Sobre as licenças, com precisão, porque eu mesmo entendi errado na minha cabeça uma vez: o Bernstein é Apache-2.0. Apache-2.0 permite uso comercial; não obriga ninguém a ser não comercial. Que o Bernstein fosse, seja, e no que depender de mim continue sendo um projeto open-source livre, é uma posição e uma intenção, não uma condição da licença.

Um projeto livre ainda pode ter patrocinadores, publicidade, colocações de parceria, integrações relevantes. Se você conhece um laboratório de IA ou empresa nesse espaço para quem isso possa fazer sentido de verdade, me indique a eles. Vou tentar montar um pacote de parceria decente nas próximas semanas, e então vemos o que dá para fazer com isso. Até então tem o GitHub Sponsors, que existe e é pequeno.

Uma regra firme. Nenhuma integração paga nunca compra um merge. Se uma empresa quer pagar por um trabalho de integração específico, o trabalho passa pelo processo técnico normal, o maintainer e a comunidade mantêm o direito de dizer não, e uma decisão técnica não muda porque apareceu um orçamento. Se algum trabalho pago for de fato aprovado, o valor iria para hospedagem, custos operacionais e as pessoas que sustentam o projeto. Isso não é uma política de remuneração; não há percentuais; é apenas para onde o dinheiro iria.

E mais uma coisa humana. Se uma oportunidade paga real algum dia aparecer em torno do Bernstein, as pessoas que já colocaram trabalho e que eu já conheço obviamente estarão entre as primeiras que eu vou olhar. Isso não é uma promessa, nem um programa, nem "trabalhe de graça agora, seja contratado depois". É só a lógica normal de que, se eu já gosto de como alguém pensa e trabalha, vou lembrar dessa pessoa antes de um estranho.

## Quem está observando, e por que isso importa

Não podemos, claro, publicar todo o nosso pipeline de analytics, mas por todas as aparências não somos os únicos lendo. Gente de grandes empresas de TI e laboratórios de IA aparece no site com bastante regularidade e faz perguntas bem específicas sobre a documentação. Os concorrentes, vamos assumir, também não estão dormindo.

E, aliás: pelo nosso analytics passivo, tirando o ruído óbvio de bots, o Bernstein hoje roda regularmente em pelo menos umas 3.000 máquinas pelo mundo. Acho isso bem respeitável. O Bernstein não envia telemetria de produto por padrão e esse número não vem de nenhuma; é um sinal passivo, e é tudo que vou dizer sobre o método.

Então a situação já é interessante por si só. Estamos construindo um produto livre que as pessoas realmente usam, que atrai a atenção de engenheiros, laboratórios de IA e grandes empresas, e qualquer usuário pode pegar o código-fonte e ser dono dele. Minha opinião, claramente uma opinião: quando você consegue uma ferramenta séria de graça e ainda é dono do código, fica difícil explicar por que alguém pagaria dezenas de milhares de dólares por mês por algo parecido.

Quero que esse projeto pareça grande. Não para podermos dizer que somos ótimos, mas para que todo mundo trabalhando aqui entenda que está fazendo algo que pode importar bem além de um pull request. A régua que tenho na cabeça é mais ou menos Ansible, Kubernetes, Terraform. Não "Bernstein é o próximo Kubernetes". Mais assim: se o Bernstein algum dia se tornar a implementação de referência na sua categoria, não quero que a gente olhe para trás pensando que fizemos isso com pressa. Devíamos fazer isso bem.

## Algumas ressalvas honestas

Não posso prometer a cadência. "Mais ou menos a cada duas semanas" é uma meta, não um nível de serviço. É totalmente possível que a primeira publicação saia em duas semanas, depois passe um mês, depois descubramos que queríamos automatizar mais três coisas primeiro, e tudo que ia levar semanas discretamente leve até o fim do ano calendário. Vamos tentar. Não vou prometer o que não tenho certeza de conseguir entregar.

Por volta de meados de novembro, se Deus quiser e a capacidade permitir, gostaria de fazer uma chamada curta no Zoom. Só para nos conhecermos, e conversar sobre onde cada um de vocês quer chegar. Sem obrigação de comparecer, ainda sem data exata.

Respeitamos que as pessoas neste projeto falam idiomas diferentes, então o original fica em inglês e algumas traduções seguem abaixo, nos comentários. É uma tentativa pequena de deixar as coisas um pouco melhores para vocês, nada mais.

Do meu lado, isso é basicamente eu e o gato. Do lado de vocês, é todo o resto.

Compartilhar é se importar (sharing is caring). Tento devolver o máximo que posso pelo trabalho de vocês, e só por vocês estarem aqui. Acreditem, eu gosto muito de todos vocês. Entre outras coisas, é isso que me faz levantar de manhã e sentar na frente do computador, depois do que descubro que quinze horas se passaram. Vamos ver o que construímos com tudo isso.

Alex
