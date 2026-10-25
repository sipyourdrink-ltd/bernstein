Amigos, hola.

He estado pensando un poco, y creo que no me va a matar si, más o menos cada dos semanas, la página de Bernstein en LinkedIn menciona, personalmente, a cada colaborador que quiera ser mencionado, y les da las gracias a todos en una sola publicación conjunta.

Una publicación. No una por persona. Nuestros seguidores no necesitan diez publicaciones seguidas, y yo no necesito un segundo trabajo escribiéndolas. Pero dentro de esa única publicación quiero intentar decir, por persona, qué hiciste realmente, de modo que si un posible empleador la lee entienda lo que construiste en un proyecto real, y para que no te dé vergüenza compartirla de nuevo.

Por cierto, esto ya lo empezamos. La #5524 tiene la página de LinkedIn y a bastantes de vosotros diciendo "vale, mencióname", y la #6229 fue el primer intento de un agradecimiento conjunto. Así que esto no es nada nuevo. Es solo que estoy intentando dejar de montar esto a mano cada vez. Los que ya dijisteis que sí en la #5524 no necesitáis repetirlo; ya os tengo anotados.

## De qué va esto

Todo el proceso, desde tu lado:

1. Hiciste un trabajo significativo en Bernstein en los últimos tres meses.
2. Sigues la página del proyecto: https://www.linkedin.com/company/bernstein-run/
3. De vez en cuando la página publica una publicación conjunta que menciona a las personas que se apuntaron (opt in) y lo que hicieron.
4. La compartes de nuevo si quieres, idealmente etiquetando a Bernstein.
5. El proyecto obtiene un alcance que de otro modo no tendría. Tú obtienes un registro público y profesional que no has escrito tú mismo sobre ti mismo.

Eso es todo. No hay un sexto paso.

Si ahora mismo estás buscando trabajo, hay una alternativa: puedo escribirte una recomendación de LinkedIn en su lugar, desde mi perfil (https://www.linkedin.com/in/alex-chernysh/). No es ninguna molestia. Pero quiero ser sincero sobre el compromiso: la recomendación se queda en mi perfil y te ayuda a ti; el repost de la publicación conjunta te ayuda a ti y al proyecto a la vez. Y honestamente, casi nadie se desplaza hasta la sección de Recomendaciones de todos modos. Así que usa la recomendación sobre todo si estás buscando activamente, y el repost en el resto de los casos. Si quieres las dos cosas, dilo.

Sobre el valor de todo esto. Si de verdad hiciste el trabajo, no lo escondas. Pon Bernstein en tu CV, en tu portfolio, donde corresponda. Yo lo hago, algunos de vosotros ya lo hacéis, y creo que es una buena señal: una contribución fusionada (merged) en un proyecto de código abierto serio, que puedes explicar línea por línea, vale más que una línea del currículum que diga "sabe Python". Esa es mi opinión, no un estudio. Tampoco es una promesa de nada: esto es un proyecto voluntario, nadie está contratando, y prefiero decirlo claramente ahora antes que dejar que alguien le dé otra lectura más adelante.

## Cómo decir que sí

Envía un correo. Dirección y asunto exactamente así:

> **To:** forte@bernstein.run
> **Subject:** `BRNSTN-PR-LNKD`

En el cuerpo, unas líneas que un script pueda leer:

```
GITHUB=your-github-login
OPT_IN=YES
RECOMMENDATION=NO
```

Pon RECOMMENDATION=YES si quieres la recomendación de LinkedIn en lugar de, o además de, la publicación. Añade una línea NOTE= si hay algo que deba saber (por ejemplo, que quieres ver el texto antes de que se publique; un par de vosotros lo pidió y no hay problema). Para darte de baja más adelante, misma dirección, mismo asunto, OPT_IN=NO.

Por favor, no cambies la línea de asunto. El parser necesita exactamente esa cadena. El volumen de correo entrante es tal que esto no es cuestión de gustos; un mensaje con otro asunto cae en la cola general y no puedo prometer que llegue a verlo alguna vez.

## El filtro blando, y por qué es blando

Para decidir quién entra en una publicación dada sin que yo tenga que leer cada PR a mano, hay un filtro mecánico: aproximadamente 1.000 líneas añadidas en pull requests fusionados en los últimos 30 días, excluyendo archivos generados. No es un KPI. No es una métrica de calidad. No es un juicio sobre nadie. Es una forma barata de que un script reúna un grupo cada par de semanas sin que haya una persona en el bucle.

Y no es una valla infranqueable. Aquí hay gente que hace trabajo de seguridad, revisiones, documentación, benchmarks, tareas de release y depuración que pueden ser enormemente valiosas con cuarenta líneas de diff. Si hiciste un trabajo significativo y por algún motivo no llegaste a mil, escribe a la misma dirección con el mismo asunto y dilo. No somos monstruos; ya encontraremos algo. La respuesta habitual es que te buscamos la siguiente tarea y superas ese desafortunado millar con la siguiente contribución.

Los nombres en la lista periódica van en orden alfabético. No por número de PRs, ni por a quién le tenga más simpatía. Alfabético.

## GitHub Discussions y la página de LinkedIn no compiten entre sí

Hacen trabajos distintos. Para noticias rápidas (un release, un cambio, una actualización de la comunidad), la página de LinkedIn es el canal más rápido. GitHub Discussions es donde participas de verdad en el proyecto: ves el contexto, discutes, propones, encuentras la respuesta, y queda registrado donde la siguiente persona pueda encontrarlo. Si quieres las dos cosas, la noticia y la profundidad, sigue ambos. Las decisiones siguen ocurriendo en issues y pull requests; eso no ha cambiado.

## Por qué hago esto en absoluto

Muchos de vosotros sois jóvenes. Algunos no tan jóvenes. Pero para alguien que empieza, un proyecto de código abierto de verdad es una oportunidad bastante rara para probar no solo código, sino revisión, arquitectura, testing, documentación, seguridad, releases, coordinación, incluso algo de escritura pública, y para tomar decisiones que tienen consecuencias. Si yo hubiera podido trabajar en algo así mientras estaba en la universidad, mi vida probablemente habría sido bastante distinta. Así que quiero apoyar a quienes empiezan a construir todo lo que pueda.

Mi modelo es simple: cortar los hitos en trozos que una persona pueda sostener, dar a alguien una tarea real, dejar la decisión en sus manos siempre que pueda ser suya, no quitarle la tarea tras el primer error, y dejar que la gente asuma más responsabilidad a medida que avanza.

A los que estáis estudiando algo relacionado con informática ahora mismo, o simplemente empezando: os veo. Estoy con vosotros. Lo estáis haciendo bien.

Una opinión más, claramente etiquetada como tal. Saber programar se está convirtiendo en algo básico, como saber escribir a máquina. Saber pensar, ver el compromiso (trade-off), tomar una decisión que puedas defender, eso sigue siendo raro, y en la era de la IA sospecho que se vuelve más raro y más valioso, no menos. Este es un lugar para practicar precisamente eso.

## Cómo se dirige el proyecto, por ahora

Hasta finales de este año natural sigo siendo el único mantenedor (maintainer). Es deliberado. No significa que el mantenedor decida todo. La propiedad es mía; no creo que deba elegir personalmente cada coma de la arquitectura. Si viniste a hacer un PR, quiero que pienses, no que adivines qué quiere el mantenedor.

Cuando tenga capacidad, y ahora mismo estoy algo enterrado en otras cosas, quiero intentar dar algo más de forma al trabajo, con "departamentos" virtuales, como los tiene una empresa normal. Todo voluntario, sin obligaciones, sin burocracia por el gusto de tenerla. Para empezar solo veo dos: I+D (R&D), que ya vive en issues y pull requests y necesita poco de mí, y PR / outreach, que probablemente supervise algo más de cerca, porque la comunicación sin estructura se convierte en "deberíamos hacer eso en algún momento" más rápido que cualquier otra cosa que conozca.

Más adelante, si hay suficiente gente activa, podrían aparecer roles: un director de desarrollo, uno de operaciones, uno de seguridad, uno de QA y documentación. No estoy creando esos títulos por adelantado. El número de personas determina la estructura, no al revés. Nadie necesita una corporación de seis personas.

## El dinero, porque alguien siempre pregunta

Cuando alguien pregunta si hay una oportunidad remunerada para trabajar en Bernstein, a veces suelto una risita nerviosa. Bernstein no genera dinero. Gasta el mío. Ni siquiera cuento mi tiempo; simplemente observo lo que sale de la cuenta bancaria, en un país bastante caro, mientras estoy entre trabajos y las circunstancias probablemente van a darme al menos otro par de semanas en este proyecto. Así que lo que estoy haciendo no es trabajo no remunerado. Es trabajo a pérdida. Me gusta el trabajo. Eso es, en general, por lo que estamos todos aquí.

Gratis para el usuario no significa gratis para el mantenedor. La infraestructura detrás de esto cuesta del orden de un par de miles de dólares al mes, y esa factura llega tanto si alguien publica algo en LinkedIn como si no.

Sobre las licencias, con precisión, porque yo mismo lo entendí mal en mi cabeza una vez: Bernstein es Apache-2.0. Apache-2.0 permite el uso comercial; no obliga a nadie a ser no comercial. Que Bernstein fuera, sea, y en lo que a mí respecta siga siendo un proyecto de código abierto libre, es una postura y una intención, no una condición de la licencia.

Un proyecto libre puede seguir teniendo patrocinadores, publicidad, colaboraciones de partnership, integraciones relevantes. Si conoces a un laboratorio de IA o a una empresa de este espacio para quien esto pueda tener sentido de verdad, indícales que se pongan en contacto conmigo. Intentaré tener listo un paquete de partnership decente en las próximas semanas, y luego veremos qué se puede hacer con él. Hasta entonces está GitHub Sponsors, que existe y es pequeño.

Una regla firme. Ninguna integración remunerada compra nunca un merge. Si una empresa quiere pagar por un trabajo de integración específico, ese trabajo pasa por el proceso técnico normal, el mantenedor y la comunidad conservan el derecho a decir que no, y una decisión técnica no cambia porque haya aparecido un presupuesto. Si alguna vez se aprueba de verdad algún trabajo remunerado, lo recaudado iría destinado al hosting, a los costes de funcionamiento y a las personas que sostienen el proyecto. Eso no es una política de compensación; no hay porcentajes; es solo adónde iría el dinero.

Y una cosa más, humana. Si alguna vez surge una oportunidad remunerada real alrededor de Bernstein, las personas que ya han puesto trabajo y a quienes ya conozco serán obviamente de las primeras en las que piense. Eso no es una promesa, ni un programa, ni "trabaja gratis ahora, que te contraten después". Es solo la lógica normal de que si ya me gusta cómo piensa y trabaja alguien, lo recordaré antes que a un desconocido.

## Quién está mirando, y por qué importa

No podemos, por supuesto, publicar toda nuestra tubería de analítica, pero por todas las apariencias no somos los únicos que leen. Gente de grandes empresas de IT y de laboratorios de IA aparece por el sitio con bastante regularidad y hace preguntas bastante específicas sobre la documentación. Los competidores, asumamos, tampoco están dormidos.

Y, por cierto: según nuestra analítica pasiva, quitando el ruido evidente de bots, Bernstein ahora funciona con regularidad en al menos unas 3.000 máquinas por todo el mundo. Creo que eso es bastante respetable. Bernstein no envía telemetría de producto por defecto y este número no proviene de ninguna; es una señal pasiva, y eso es todo lo que voy a decir sobre el método.

Así que la situación ya es interesante de por sí. Estamos construyendo un producto libre que la gente realmente usa, que atrae la atención de ingenieros, laboratorios de IA y grandes empresas, y cualquier usuario puede coger el código fuente y ser dueño de él. Mi opinión, claramente una opinión: cuando puedes conseguir gratis una herramienta seria y ser dueño del código, se vuelve difícil explicar por qué alguien pagaría decenas de miles de dólares al mes por algo similar.

Quiero que este proyecto se sienta grande. No para que podamos decirle a la gente que somos estupendos, sino para que todos los que trabajan aquí entiendan que están haciendo algo que puede importar mucho más allá de un solo pull request. El listón que tengo en la cabeza es más o menos Ansible, Kubernetes, Terraform. No "Bernstein es el próximo Kubernetes". Más bien: si Bernstein alguna vez llega a ser la implementación de referencia en su categoría, no quiero que miremos atrás pensando que lo hicimos con prisas. Deberíamos hacerlo bien.

## Unas cuantas advertencias sinceras

No puedo prometer la cadencia. "Más o menos cada dos semanas" es un objetivo, no un nivel de servicio. Es totalmente posible que la primera publicación toque en dos semanas, luego pase un mes, luego resulte que primero queríamos automatizar tres cosas más, y todo lo que iba a tardar semanas termine tardando silenciosamente hasta finales del año natural. Lo intentaremos. No te voy a prometer algo que no estoy seguro de poder cumplir.

Hacia mediados de noviembre, si Dios quiere y la capacidad lo permite, me gustaría hacer una breve videollamada por Zoom. Solo para conocernos, y para hablar de hacia dónde quiere ir cada uno de vosotros. Sin obligación de asistir, todavía sin fecha exacta.

Respetamos que en este proyecto la gente hable idiomas distintos, así que el original se queda en inglés y a continuación, en los comentarios, siguen algunas traducciones. Es un pequeño intento de hacer las cosas un poco más agradables para vosotros, nada más.

Por mi parte, esto es sobre todo yo y el gato. Por la vuestra, sois todos los demás.

Compartir es cuidar (sharing is caring). Intento devolveros todo lo que puedo por vuestro trabajo y simplemente por estar aquí. Creedme, os tengo mucho cariño a todos. Entre otras cosas, es lo que me hace levantarme por la mañana y sentarme delante del ordenador, tras lo cual descubro que han pasado quince horas. Veamos qué construimos con todo esto.

Alex
