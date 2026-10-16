import { createHash } from "node:crypto";

export class HashRing {
    constructor(shardIds, vnodes) {
        this.shardIds = shardIds;
        this.vnodes = vnodes;
        this._ring = [];
        this._initRing();
    }

    _initRing() {
        const ring = [];
        for (const shardId of this.shardIds) {
            for (let i = 0; i < this.vnodes; i++) {
                const vnodeKey = `${shardId}#${i}`;
                const hashVal = this._hashKey(vnodeKey);
                ring.push({ hashVal, shardId });
            }
        }
        ring.sort((a, b) => {
            if (a.hashVal < b.hashVal) return -1;
            if (a.hashVal > b.hashVal) return 1;
            return 0;
        });
        this._ring = ring;
    }

    _hashKey(key) {
        const hash = createHash("sha256").update(key, "utf8").digest();
        const first8BytesHex = hash.toString("hex").slice(0, 16);
        return BigInt("0x" + first8BytesHex);
    }

    lookup(key) {
        if (this._ring.length === 0) {
            throw new Error("no shards in ring");
        }
        const hashVal = this._hashKey(key);

        let low = 0;
        let high = this._ring.length;

        while (low < high) {
            const mid = Math.floor((low + high) / 2);
            if (this._ring[mid].hashVal < hashVal) {
                low = mid + 1;
            } else {
                high = mid;
            }
        }

        if (low >= this._ring.length) {
            low = 0;
        }
        return this._ring[low].shardId;
    }
}
