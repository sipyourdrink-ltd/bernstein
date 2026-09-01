## Record resolved endpoint identity per spawn

Every agent spawn now records the resolved executor identity (adapter name, model, normalized base_url origin, and endpoint-profile name) in the run journal. Operators can answer which model server produced this work from the run record alone, without reconstructing it from config history. The endpoint identity is visible in bernstein replay output and bound in the signed run receipt.

(#4908)