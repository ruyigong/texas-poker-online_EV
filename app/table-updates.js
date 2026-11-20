function createTableUpdateReceiver(resync) {
  let current = null;
  let waitingForSnapshot = false;

  function requestSnapshot() {
    if (!waitingForSnapshot) {
      waitingForSnapshot = true;
      resync();
    }
    return null;
  }

  return message => {
    if (message.type === "state") {
      current = message;
      waitingForSnapshot = false;
      return current;
    }
    if (message.type !== "state_delta") return null;
    if (waitingForSnapshot) return null;
    if (!current || message.baseRevision !== current.revision ||
        message.revision !== current.revision + 1) {
      return requestSnapshot();
    }
    const next = JSON.parse(JSON.stringify(current));
    try {
      for (const [kind, path, value, removeCount, additions] of message.operations) {
        const parentPath = kind === "splice" ? path : path.slice(0, -1);
        let parent = next;
        for (const key of parentPath) {
          if (!Object.prototype.hasOwnProperty.call(parent, key)) throw new Error("Missing update path");
          parent = parent[key];
        }
        if (kind === "splice") {
          if (!Array.isArray(parent) || value < 0 || value > parent.length ||
              removeCount < 0 || value + removeCount > parent.length) {
            throw new Error("Invalid list update");
          }
          parent.splice(value, removeCount, ...additions);
        } else {
          if (!path.length) throw new Error("Missing field path");
          const key = path[path.length - 1];
          if (kind === "set") {
            Object.defineProperty(parent, key, {value, writable: true, enumerable: true, configurable: true});
          } else if (kind === "delete") {
            delete parent[key];
          } else {
            throw new Error("Unknown update operation");
          }
        }
      }
    } catch (error) {
      return requestSnapshot();
    }
    next.revision = message.revision;
    current = next;
    return current;
  };
}
