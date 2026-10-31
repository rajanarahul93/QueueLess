// Every open live stream registers its AbortController here, so logout can close them all.
// The streams themselves arrive with the live ticket and the staff console.
const controllers = new Set<AbortController>();

export function registerStream(controller: AbortController): () => void {
  controllers.add(controller);
  return () => controllers.delete(controller);
}

export function abortAllStreams(): void {
  controllers.forEach((controller) => controller.abort());
  controllers.clear();
}
