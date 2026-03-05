# Mocking Patterns Reference

Framework-specific mocking guides. Use this when you need to mock dependencies in tests
and aren't sure about the syntax or best practices for the project's test framework.

## Python — pytest

### unittest.mock.patch (most common)
```python
from unittest.mock import patch, MagicMock

# Patch a module-level function
@patch('myapp.services.send_email')
def test_register_sends_email(mock_send):
    register_user({"email": "test@example.com"})
    mock_send.assert_called_once_with("test@example.com", subject="Welcome")

# Patch with return value
@patch('myapp.db.get_user')
def test_get_user_profile(mock_get):
    mock_get.return_value = {"id": 1, "name": "Alice"}
    result = get_profile(1)
    assert result["name"] == "Alice"

# Patch with side effect (raise exception)
@patch('myapp.api.fetch_data')
def test_handles_api_error(mock_fetch):
    mock_fetch.side_effect = ConnectionError("timeout")
    result = safe_fetch()
    assert result is None

# Context manager form
def test_something():
    with patch('myapp.services.send_email') as mock_send:
        mock_send.return_value = True
        result = register_user(data)
        assert result.email_sent is True
```

### pytest monkeypatch (simpler)
```python
def test_reads_env_var(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key-123")
    config = load_config()
    assert config.api_key == "test-key-123"

def test_with_temp_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("myapp.config.DATA_DIR", str(tmp_path))
    save_data("test")
    assert (tmp_path / "test.json").exists()
```

### pytest fixtures (for reusable mocks)
```python
import pytest
from unittest.mock import MagicMock

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.query.return_value = [{"id": 1, "name": "Test"}]
    db.insert.return_value = 42
    return db

def test_list_users(mock_db):
    result = list_users(mock_db)
    assert len(result) == 1
    mock_db.query.assert_called_once()

def test_create_user(mock_db):
    user_id = create_user(mock_db, "Alice")
    assert user_id == 42
```

### Mocking async (Python 3.8+)
```python
from unittest.mock import AsyncMock, patch

@patch('myapp.api.fetch', new_callable=AsyncMock)
async def test_async_fetch(mock_fetch):
    mock_fetch.return_value = {"data": "test"}
    result = await get_data()
    assert result["data"] == "test"
```

## JavaScript — Jest

### jest.mock (module-level)
```javascript
// Mock an entire module
jest.mock('./database');
const db = require('./database');

test('creates user in database', async () => {
  db.insert.mockResolvedValue({ id: 1 });
  const user = await createUser({ name: 'Alice' });
  expect(user.id).toBe(1);
  expect(db.insert).toHaveBeenCalledWith('users', { name: 'Alice' });
});
```

### jest.spyOn (partial mock)
```javascript
const utils = require('./utils');

test('logs errors', () => {
  const spy = jest.spyOn(console, 'error').mockImplementation();
  processData(null);
  expect(spy).toHaveBeenCalledWith(expect.stringContaining('invalid'));
  spy.mockRestore();
});
```

### Manual mocks (__mocks__ directory)
```
src/
  api.js
  __mocks__/
    api.js          ← Jest auto-uses this when jest.mock('./api') is called
```

```javascript
// __mocks__/api.js
module.exports = {
  fetchUser: jest.fn().mockResolvedValue({ id: 1, name: 'Mock User' }),
  saveUser: jest.fn().mockResolvedValue(true),
};
```

### Mocking timers
```javascript
jest.useFakeTimers();

test('debounce waits 300ms', () => {
  const fn = jest.fn();
  const debounced = debounce(fn, 300);

  debounced();
  expect(fn).not.toHaveBeenCalled();

  jest.advanceTimersByTime(300);
  expect(fn).toHaveBeenCalledTimes(1);
});
```

### Mocking fetch / HTTP
```javascript
// Option 1: jest.mock global fetch
global.fetch = jest.fn().mockResolvedValue({
  ok: true,
  json: () => Promise.resolve({ data: 'test' }),
});

// Option 2: Use msw (Mock Service Worker) for integration tests
// setupTests.js
const { setupServer } = require('msw/node');
const { rest } = require('msw');

const server = setupServer(
  rest.get('/api/users', (req, res, ctx) => {
    return res(ctx.json([{ id: 1, name: 'Alice' }]));
  })
);

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
```

## TypeScript — Vitest

### vi.mock (similar to Jest)
```typescript
import { vi, describe, test, expect } from 'vitest';
import { getUser } from './database';

vi.mock('./database', () => ({
  getUser: vi.fn(),
}));

test('returns user profile', async () => {
  vi.mocked(getUser).mockResolvedValue({ id: 1, name: 'Alice' });
  const profile = await fetchProfile(1);
  expect(profile.name).toBe('Alice');
});
```

### vi.spyOn
```typescript
import { vi } from 'vitest';

const spy = vi.spyOn(Date, 'now').mockReturnValue(1234567890);
// ... test with fixed time ...
spy.mockRestore();
```

## Go — Interface-Based Testing

Go uses interfaces for test doubles — no mocking library needed for most cases.

### Interface + test double
```go
// Production interface
type UserStore interface {
    GetUser(id int) (*User, error)
    SaveUser(u *User) error
}

// Test double
type mockUserStore struct {
    users map[int]*User
    err   error
}

func (m *mockUserStore) GetUser(id int) (*User, error) {
    if m.err != nil {
        return nil, m.err
    }
    return m.users[id], nil
}

func (m *mockUserStore) SaveUser(u *User) error {
    if m.err != nil {
        return m.err
    }
    m.users[u.ID] = u
    return nil
}

func TestGetProfile(t *testing.T) {
    store := &mockUserStore{
        users: map[int]*User{1: {ID: 1, Name: "Alice"}},
    }
    profile, err := GetProfile(store, 1)
    if err != nil {
        t.Fatal(err)
    }
    if profile.Name != "Alice" {
        t.Errorf("got %q, want Alice", profile.Name)
    }
}
```

### httptest (for HTTP handlers)
```go
func TestHealthEndpoint(t *testing.T) {
    req := httptest.NewRequest("GET", "/health", nil)
    w := httptest.NewRecorder()

    HealthHandler(w, req)

    if w.Code != 200 {
        t.Errorf("got status %d, want 200", w.Code)
    }
}
```

### testify/mock (when interface doubles get complex)
```go
import "github.com/stretchr/testify/mock"

type MockStore struct {
    mock.Mock
}

func (m *MockStore) GetUser(id int) (*User, error) {
    args := m.Called(id)
    return args.Get(0).(*User), args.Error(1)
}

func TestGetProfile(t *testing.T) {
    store := new(MockStore)
    store.On("GetUser", 1).Return(&User{Name: "Alice"}, nil)

    profile, err := GetProfile(store, 1)
    assert.NoError(t, err)
    assert.Equal(t, "Alice", profile.Name)
    store.AssertExpectations(t)
}
```

## Rust — Traits + mockall

### Trait-based test doubles
```rust
trait UserRepository {
    fn get_user(&self, id: i32) -> Result<User, Error>;
}

struct MockRepo {
    user: Option<User>,
}

impl UserRepository for MockRepo {
    fn get_user(&self, _id: i32) -> Result<User, Error> {
        self.user.clone().ok_or(Error::NotFound)
    }
}

#[test]
fn test_get_profile() {
    let repo = MockRepo { user: Some(User { name: "Alice".into() }) };
    let profile = get_profile(&repo, 1).unwrap();
    assert_eq!(profile.name, "Alice");
}
```

### mockall crate (for complex mocking)
```rust
use mockall::automock;

#[automock]
trait UserRepo {
    fn get_user(&self, id: i32) -> Result<User, Error>;
}

#[test]
fn test_with_mockall() {
    let mut mock = MockUserRepo::new();
    mock.expect_get_user()
        .with(eq(1))
        .returning(|_| Ok(User { name: "Alice".into() }));

    let profile = get_profile(&mock, 1).unwrap();
    assert_eq!(profile.name, "Alice");
}
```

## C# — Moq

```csharp
using Moq;

[Fact]
public async Task GetProfile_ReturnsUser()
{
    // Arrange
    var mockRepo = new Mock<IUserRepository>();
    mockRepo.Setup(r => r.GetUserAsync(1))
            .ReturnsAsync(new User { Id = 1, Name = "Alice" });

    var service = new UserService(mockRepo.Object);

    // Act
    var profile = await service.GetProfileAsync(1);

    // Assert
    Assert.Equal("Alice", profile.Name);
    mockRepo.Verify(r => r.GetUserAsync(1), Times.Once);
}

// Mock with exception
mockRepo.Setup(r => r.GetUserAsync(999))
        .ThrowsAsync(new NotFoundException());

// Mock with callback
mockRepo.Setup(r => r.SaveAsync(It.IsAny<User>()))
        .Callback<User>(u => Assert.NotNull(u.Email))
        .ReturnsAsync(true);
```

## When to Use Each Approach

| Scenario | Recommended Approach |
|----------|---------------------|
| External HTTP API | Mock the HTTP client (not the API) |
| Database queries | Mock the repository/data access layer |
| File system | Use temp directory (pytest `tmp_path`, Go `t.TempDir()`) |
| Time-dependent logic | Mock `Date.now()` / `time.Now()` |
| Environment variables | Set in test setup, restore in teardown |
| Random values | Seed the RNG or inject a mock generator |
| Third-party SDK | Mock at the SDK client level |
| Internal pure functions | DON'T MOCK — test directly |
