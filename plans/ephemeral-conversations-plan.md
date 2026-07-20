# Ephemeral Conversations — Design Plan

## Overview

A new conversation mode where message data lives primarily on the **client** and only passes through the server transiently during execution. The server never stores conversation history long-term. An optional **pickup window** (default 24h) allows the client to reconnect and retrieve the final response if the connection dropped mid-stream.

### Terminology

| Term | Meaning |
|------|---------|
| **Ephemeral mode** | The client owns the conversation history; the server holds data only for the duration of a run (+ pickup window). |
| **Pickup window** | A configurable TTL (e.g. 24 hours) during which the server retains transient run data so a disconnected client can retrieve the result. After expiry, data is hard-deleted. |
| **Persistent mode** | Current default — the server stores full conversation history indefinitely. |

---

## 1. How It Works — High Level

```
┌──────────────────────────────────────────────────────────────────────┐
│                          CLIENT (iOS / Android / Web)                │
│                                                                      │
│   ┌─────────────────────────────────────────────────────────────┐    │
│   │  Local Conversation Store                                    │    │
│   │  • Full message history (user + assistant + tool)             │    │
│   │  • Stored in: CoreData / Room / IndexedDB / in-memory        │    │
│   │  • Survives app restart (optional, per-client decision)      │    │
│   └─────────────────────────────────────────────────────────────┘    │
│                          │                                           │
│          createRun(      │     POST /api/agent-runtime/runs/        │
│            messages: [entire history + new message],                 │
│            ephemeral: true                                          │
│          )               ▼                                           │
└──────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          SERVER (django_agent_runtime)               │
│                                                                      │
│   1. Receive messages in request body (NOT loaded from DB)           │
│   2. Create AgentRun with input={messages: [...]}                    │
│   3. Do NOT create/update AgentConversation messages                 │
│   4. Run agent → stream SSE events → emit result                    │
│   5. Store result transiently (pickup window TTL)                    │
│   6. Cleanup job hard-deletes after TTL expires                     │
│                                                                      │
│   What IS stored:          What is NOT stored:                       │
│   • AgentRun (transient)   • AgentConversation.messages              │
│   • AgentEvent (transient) • PersistenceMessage                      │
│   • Run output (transient) • Normalized Message records              │
│                            • Conversation history                    │
└──────────────────────────────────────────────────────────────────────┘
```

**Key insight:** In ephemeral mode, the client sends the **full conversation history** with every run request (like the OpenAI API pattern), rather than relying on the server to reconstruct history from previous runs.

---

## 2. Server Changes (`django_agent_runtime`)

### 2.1 New `message_storage_mode`: `"ephemeral"`

**File: `agent/django_agent_runtime/models/base.py`**

Add `EPHEMERAL = "ephemeral"` to `MessageStorageMode`:

```python
class MessageStorageMode(models.TextChoices):
    JSON = "json", "JSON (in AgentRun)"
    NORMALIZED = "normalized", "Normalized (Message model)"
    EPHEMERAL = "ephemeral", "Ephemeral (client-owned, transient server)"
```

**File: `agent/django_agent_runtime/conf.py`**

Add new settings:

```python
# Ephemeral mode settings
EPHEMERAL_PICKUP_WINDOW_SECONDS: int = 86400  # 24 hours default
EPHEMERAL_CLEANUP_INTERVAL_SECONDS: int = 3600  # Run cleanup every hour
```

### 2.2 Agent Definition Support

**File: `agent/django_agent_runtime/models/definitions.py`**

The existing `message_storage_mode` field on `AgentDefinition` already supports choices — it will automatically pick up the new `EPHEMERAL` choice. Agents can be configured per-agent:

```python
# In admin or via API:
agent_def.message_storage_mode = "ephemeral"
```

### 2.3 API Changes

**File: `agent/django_agent_runtime/api/views.py` — `BaseAgentRunViewSet.create()`**

When ephemeral mode is active:

1. **Skip conversation auto-creation** — or create a lightweight "ephemeral" conversation record (no messages stored) solely for run grouping and pickup.
2. **Mark the run as ephemeral** — `run.metadata["ephemeral"] = True`
3. **Set TTL on the run** — `run.metadata["expires_at"] = now + pickup_window`
4. Accept a new optional field `ephemeral: true` in the request body to override per-request.

```python
# In create():
is_ephemeral = (
    data.get("ephemeral", False) or
    self._get_agent_storage_mode(data["agent_key"]) == "ephemeral"
)

if is_ephemeral:
    # Create minimal conversation (no history stored)
    conversation = AgentConversation.objects.create(
        agent_key=data["agent_key"],
        user=request.user if request.user.is_authenticated else None,
        metadata={"ephemeral": True},
    )
    metadata["ephemeral"] = True

### 2.4 Runner Changes

**File: `agent/django_agent_runtime/runtime/runner.py`**

The runner's `_load_conversation_history()` currently loads history from previous runs in the DB. In ephemeral mode:

1. **Skip DB history loading** — the client already sent the full history in the request.
2. **Skip normalized message creation** — `_maybe_create_normalized_messages()` should no-op.
3. **Skip title generation** — `_maybe_generate_conversation_title()` should no-op (or the client can set its own title locally).

```python
async def _load_conversation_history(self, conversation_id, new_messages, current_run_id):
    # Check if this run is ephemeral (from metadata)
    # If so, the client already sent the full history — use as-is
    if self._is_ephemeral:
        debug_print("Ephemeral mode: using client-provided messages as full context")
        return new_messages

    # ... existing history loading logic ...
```

In `_finalize_run()`:
```python
async def _finalize_run(self, run_id, ctx, result):
    # ... existing finalization ...

    if not ctx.metadata.get("ephemeral"):
        # Only create normalized messages and titles for persistent conversations
        await self._maybe_create_normalized_messages(...)
        await self._maybe_generate_conversation_title(...)
```

### 2.5 Pickup Window & Cleanup

**New file: `agent/django_agent_runtime/management/commands/cleanup_ephemeral.py`**

A management command (also runnable as a periodic task) that hard-deletes expired ephemeral data:

```python
class Command(BaseCommand):
    help = "Delete expired ephemeral run data"

    def handle(self, *args, **options):
        from django.utils import timezone
        from django_agent_runtime.models import AgentRun, AgentEvent, AgentConversation

        now = timezone.now()

        # Find expired ephemeral runs
        expired_runs = AgentRun.objects.filter(
            metadata__ephemeral=True,
            metadata__expires_at__lt=now.isoformat(),
        )

        # Delete events first (FK cascade would handle this, but explicit is better)
        run_ids = list(expired_runs.values_list("id", flat=True))
        AgentEvent.objects.filter(run_id__in=run_ids).delete()

        # Delete the runs
        count = expired_runs.delete()[0]

        # Clean up orphaned ephemeral conversations (no remaining runs)
        AgentConversation.objects.filter(
            metadata__ephemeral=True,
            runs__isnull=True,  # No runs left
        ).delete()

        self.stdout.write(f"Cleaned up {count} expired ephemeral runs")
```

**Scheduling:** Add to `supervisord.conf` or use Django-Celery-Beat:
```
# Every hour
python manage.py cleanup_ephemeral
```

### 2.6 Pickup Endpoint

When a client disconnects mid-stream, it needs to retrieve the final result. The existing SSE endpoint already supports `?from_seq=N` for resuming. For ephemeral mode, add a dedicated pickup endpoint:

**New action on `BaseAgentRunViewSet`:**

```python
@action(detail=True, methods=["get"])
def pickup(self, request, pk=None):
    """
    Retrieve the result of an ephemeral run.

    Used when the client disconnected during streaming and needs
    the final result. Returns 410 Gone if the pickup window expired.
    """
    run = self.get_object()

    if not run.metadata.get("ephemeral"):
        return Response({"error": "Not an ephemeral run"}, status=400)

    expires_at = run.metadata.get("expires_at")
    if expires_at and timezone.now() > datetime.fromisoformat(expires_at):
        return Response({"error": "Pickup window expired"}, status=410)

    if not run.is_terminal:
        return Response({"status": run.status, "message": "Run still in progress"}, status=202)

    return Response({
        "id": str(run.id),
        "status": run.status,
        "output": run.output,
        "events": list(
            run.events.order_by("seq").values("event_type", "payload", "seq")
        ),
    })
```

### 2.7 Memory & Facts Interaction

When ephemeral mode is active:
- **Cross-conversation memory** (`MemoryEnabledAgent`) should still work — memories are separate from conversation data and are explicitly opted-in.
- **Conversation-scoped facts** (`ConversationMemoryStore`) should be disabled or also made ephemeral.
- The host app can decide: memory extraction can still run (extracting user preferences from ephemeral conversations), or it can be disabled via config.

Add to settings:
```python
EPHEMERAL_ALLOW_MEMORY_EXTRACTION: bool = False  # Default: don't extract from ephemeral
```

---

## 3. Client Changes — TypeScript (`agent-client`)

### 3.1 Type Changes

**File: `clients/agent-client/src/types/index.ts`**

```typescript
export interface CreateRunParams {
  agentKey: string;
  messages: Array<{ role: string; content: string }>;
  conversationId?: string | null;
  metadata?: Record<string, unknown>;
  model?: string;
  thinking?: boolean;
  supersedeFromMessageIndex?: number;
  files?: File[];
  ephemeral?: boolean;  // NEW — request ephemeral mode
}
```

### 3.2 Client Config

**File: `clients/agent-client/src/types/index.ts`**

```typescript
export interface AgentClientConfig {
  backendUrl: string;
  auth: AuthConfig;
  caseStyle?: CaseStyle;
  paths?: Partial<ApiPaths>;
  ephemeral?: boolean;  // NEW — default ephemeral mode for all runs
}
```

### 3.3 Client Implementation

**File: `clients/agent-client/src/client.ts`**

When ephemeral mode is enabled:
1. The client manages conversation history locally.
2. On `createRun()`, it sends the **full message history** (not just the new message).
3. The `getConversation()` method returns from local storage instead of calling the server.

```typescript
// In AgentClient:
private localConversations: Map<string, ConversationMessage[]> = new Map();

async createRun(params: CreateRunParams): Promise<{ run: RunResponse; handle: RunHandle }> {
  const isEphemeral = params.ephemeral ?? this.config.ephemeral ?? false;

  if (isEphemeral) {
    // Send full history in messages array
    const convId = params.conversationId;
    const existingHistory = convId ? this.localConversations.get(convId) ?? [] : [];
    const fullMessages = [...existingHistory, ...params.messages];
    params = { ...params, messages: fullMessages, ephemeral: true };
  }

  // ... rest of existing createRun logic ...
}
```

### 3.4 Local Conversation Store

Add a simple local store interface that clients can provide:

```typescript
export interface LocalConversationStore {
  /** Save messages for a conversation */
  save(conversationId: string, messages: ConversationMessage[]): void;
  /** Load messages for a conversation */
  load(conversationId: string): ConversationMessage[] | null;
  /** Delete a conversation */
  delete(conversationId: string): void;
  /** List all conversation IDs */
  list(): string[];
}
```

This is intentionally simple — each platform implements it differently:
- **Web**: `IndexedDB` or `localStorage`
- **iOS**: `CoreData`, `UserDefaults`, or file-based
- **Android**: `Room` database or `SharedPreferences`

---

## 4. Client Changes — Web (`agent-frontend`)

### 4.1 Hook Changes

**File: `clients/agent-frontend/src/hooks/useChat.js`**

When `config.ephemeral` is true:

1. **Don't call `loadConversation()`** from the server on mount — load from local storage instead.
2. **On `sendMessage()`**, pass the full message history to `createRun()`.
3. **On receiving `assistant.message`**, append to local store.
4. **`clearMessages()`** also clears local storage for that conversation.

```javascript
// In useChat:
const sendMessage = useCallback(async (content, ...) => {
  if (config.ephemeral) {
    // Build full message history from local state
    const history = messages
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({ role: m.role, content: m.content }));

    const allMessages = [...history, { role: 'user', content: content.trim() }];

    const { run, handle } = await api.client.createRun({
      agentKey: config.agentKey,
      messages: allMessages,  // Full history, not just the new message
      ephemeral: true,
      metadata: { ...config.metadata },
    });
    // ... handle events ...
  } else {
    // ... existing persistent flow ...
  }
});
```

### 4.2 Configuration

```javascript
ChatWidget.init({
  backendUrl: window.location.origin,
  agentKey: 'my-agent',
  ephemeral: true,  // NEW
  // ...
});
```

### 4.3 Sidebar / Conversation List

When ephemeral, the sidebar conversation list should:
- Load from local storage (IndexedDB/localStorage), not from the server API.
- Show a visual indicator that conversations are local-only.
- Optionally hide the sidebar entirely (since there's no server-side history).

---

## 5. Client Changes — iOS (`agent-ios`)

### 5.1 ChatViewModel Changes

**File: `clients/agent-ios/Sources/AgentClient/ViewModels/ChatViewModel.swift`**

```swift
// New property
public var isEphemeral: Bool { config.ephemeral }

// In sendMessage():
func sendMessage(_ content: String, ...) async {
    if config.ephemeral {
        // Build full history from local messages
        let history = messages
            .filter { $0.role == .user || $0.role == .assistant }
            .map { ["role": $0.role.rawValue, "content": $0.content] }

        let allMessages = history + [["role": "user", "content": content.trimmed]]

        let run = try await apiClient.createRun(
            messages: allMessages,  // Full history
            ephemeral: true,
            // ...
        )
    } else {
        // Existing single-message flow
    }
}

// In restoreConversationIfNeeded():
func restoreConversationIfNeeded() async {
    if config.ephemeral {
        // Load from local storage (CoreData/UserDefaults), NOT from server
        messages = localStore.loadMessages(for: conversationId)
        return
    }
    // ... existing server-based restore ...
}
```

### 5.2 APIClient Changes

**File: `clients/agent-ios/Sources/AgentClient/Networking/APIClient+Requests.swift`**

Add `ephemeral` parameter to `createRun()`.

### 5.3 Local Persistence

**New file: `clients/agent-ios/Sources/AgentClient/Services/LocalConversationStore.swift`**

A protocol + default implementation using `UserDefaults` (simple) or `CoreData` (robust):

```swift
public protocol LocalConversationStore {
    func save(conversationId: String, messages: [Message])
    func load(conversationId: String) -> [Message]?
    func delete(conversationId: String)
    func listConversationIds() -> [String]
}

/// Default implementation using UserDefaults (suitable for small conversations)
public class UserDefaultsConversationStore: LocalConversationStore {
    // Encode messages as JSON in UserDefaults
    // For large conversations, switch to CoreData or file-based
}
```

---

## 6. Client Changes — Android (`agent-android`)

### 6.1 Similar Pattern to iOS

- `ChatViewModel` sends full history when ephemeral.
- `ApiClient.createRun()` accepts `ephemeral` flag.
- Local storage via `Room` database or `SharedPreferences`.

### 6.2 Local Persistence

```kotlin
interface LocalConversationStore {
    suspend fun save(conversationId: String, messages: List<Message>)
    suspend fun load(conversationId: String): List<Message>?
    suspend fun delete(conversationId: String)
    suspend fun listConversationIds(): List<String>
}

// Default implementation using SharedPreferences or Room
class SharedPrefsConversationStore(context: Context) : LocalConversationStore {
    // ...
}
```

---

## 7. Migration Path & Backwards Compatibility

| Concern | Solution |
|---------|----------|
| Existing persistent conversations | No change — `message_storage_mode` defaults to `"json"` |
| Mixing modes per agent | Supported via `AgentDefinition.message_storage_mode` |
| Mixing modes per request | Supported via `ephemeral: true` in `CreateRunParams` |
| Client doesn't support ephemeral | Falls back to persistent mode (server ignores unknown fields) |
| Old server, new client sends `ephemeral: true` | Server ignores unknown field, works as persistent |
| Memory extraction | Configurable — default OFF for ephemeral |

---

## 8. Security & Privacy Considerations

1. **Data minimization** — Ephemeral mode is ideal for privacy-sensitive use cases. The server only holds data for the minimum time needed for execution.
2. **Pickup window** — The TTL ensures data doesn't linger. The cleanup job provides a hard guarantee.
3. **No server-side search** — Ephemeral conversations can't be searched or audited server-side (by design). If audit is required, use persistent mode.
4. **Client-side encryption** — Clients can optionally encrypt their local conversation store. This is a client-side concern and doesn't affect the server.
5. **Token/context limits** — Since the client sends full history each time, very long conversations will hit LLM context limits. The client should handle truncation (e.g., sliding window, summarization).

---

## 9. Implementation Order

### Phase 1 — Server Foundation
1. Add `EPHEMERAL` to `MessageStorageMode` choices
2. Add `EPHEMERAL_PICKUP_WINDOW_SECONDS` to settings
3. Modify `BaseAgentRunViewSet.create()` to handle `ephemeral` flag
4. Modify runner to skip history loading and message persistence for ephemeral runs
5. Add `cleanup_ephemeral` management command
6. Add `pickup` endpoint for disconnected clients
7. Add migration for the new `MessageStorageMode` choice

### Phase 2 — TypeScript Client (`agent-client`)
8. Add `ephemeral` to `CreateRunParams` and `AgentClientConfig`
9. Add `LocalConversationStore` interface
10. Modify `createRun()` to send full history in ephemeral mode

### Phase 3 — Web Frontend (`agent-frontend`)
11. Add `ephemeral` to `ChatWidget.init()` config
12. Modify `useChat` hook for ephemeral flow
13. Handle sidebar/conversation list for local-only conversations

### Phase 4 — iOS Client (`agent-ios`)
14. Add `ephemeral` to `ChatWidgetConfig`
15. Modify `ChatViewModel` for ephemeral flow
16. Add `LocalConversationStore` protocol + default implementation
17. Modify `APIClient` to pass `ephemeral` flag

### Phase 5 — Android Client (`agent-android`)
18. Add `ephemeral` to config
19. Modify `ChatViewModel` for ephemeral flow
20. Add `LocalConversationStore` interface + Room/SharedPrefs implementation
21. Modify `ApiClient` to pass `ephemeral` flag

---

## 10. Open Questions

1. **Should ephemeral conversations still get a server-side conversation ID?**
   - Recommendation: Yes, a lightweight one — needed for run grouping and the pickup endpoint.
   - The conversation record itself has no messages, just metadata.

2. **Maximum history size per request?**
   - The client sends full history each time. Should the server enforce a max size?
   - Recommendation: Yes, add `MAX_EPHEMERAL_MESSAGES` (default: 200) and return 413 if exceeded.

3. **File uploads in ephemeral mode?**
   - Files uploaded during ephemeral conversations should also be transient.
   - Apply the same TTL to `AgentFile` records when `metadata.ephemeral=True`.

4. **Should the pickup window be per-agent or global?**
   - Start global (`EPHEMERAL_PICKUP_WINDOW_SECONDS`), allow per-agent override later.

5. **WebSocket support?**
   - The current SSE pattern works fine for ephemeral. No changes needed.
   - If we add WebSocket support later, ephemeral works the same way.

**File: `agent/django_agent_runtime/api/serializers.py`**

Add `ephemeral` boolean field to `AgentRunCreateSerializer`:

```python
ephemeral = serializers.BooleanField(required=False, default=False)
```
