/** Fixed-size ring buffer of the most recent N items. Oldest entries fall out. */
export class RingWindow<T> {
  private items: T[] = [];

  constructor(private readonly capacity: number) {
    this.capacity = Math.max(1, capacity);
  }

  push(item: T): void {
    this.items.push(item);
    if (this.items.length > this.capacity) this.items.shift();
  }

  /** Most-recent-first snapshot. */
  values(): readonly T[] {
    return this.items.slice().reverse();
  }

  get size(): number {
    return this.items.length;
  }
}
