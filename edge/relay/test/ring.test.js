import { HashRing } from "../src/ring.js";
import { strict as assert } from "node:assert";
import { test } from "node:test";
import * as fs from "node:fs/promises";
import { fileURLToPath } from "node:url";

const VECTORS = fileURLToPath(new URL("../../../tests/fixtures/cluster/shard_ring_vectors.json", import.meta.url));

test("HashRing consistent hashing", async (t) => {
    const vectorsData = await fs.readFile(VECTORS, "utf8");
    const { vectors } = JSON.parse(vectorsData);

    for (const vector of vectors) {
        await t.test(vector.id, async () => {
            const hashRing = new HashRing(vector.shards, vector.vnodes);
            await hashRing._initRing(); // Ensure the ring is initialized

            for (const key in vector.expected_assignments) {
                const expectedShard = vector.expected_assignments[key];
                const assignedShard = await hashRing.lookup(key);
                assert.strictEqual(assignedShard, expectedShard, `Key "${key}" should be assigned to "${expectedShard}" but got "${assignedShard}"`);
            }
        });
    }
});
