## The TUI's ACTIVITY pane shows each log line once

The pane compared lines only after formatting them, and every formatted line
starts with the current time. So each agent's unchanged log tail looked new on
every poll and was printed again with a later timestamp until it scrolled out.
A single `POST /tasks` rendered as ten, and a healthy run looked like an agent
stuck in a loop. Lines are now compared as raw per-agent log text, so a line
appears once when it first shows up in that agent's tail (#6139).
