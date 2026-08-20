// Single source of truth for the API base URL.
// Kept in its own file so both api.ts and auth.ts can import it
// without creating a circular dependency.
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";
