/* ============================================================
   Clinic Booking — Application Logic
   CONFIG → Data Layer → UI Layer → Toast System → Init
   ============================================================ */

// ----------------------------------------------------------
// 1. CONFIG — Change these when connecting the real API
// ----------------------------------------------------------
const CONFIG = {
  API_BASE: "/api",
  API_KEY: "xvnO1U2Jch9UdKLANIUdQjq5yiEHb9zJ",
};

// ----------------------------------------------------------
// 2. DATA LAYER
//    All functions return { ok, status, data, headers }
// ----------------------------------------------------------

async function request(method, path, { query, body, headers = {} } = {}) {
  const url = new URLSearchParams(query).toString();
  const requestHeaders = new Headers({
    "X-API-Key": CONFIG.API_KEY,
    ...headers,
  });
  const options = {
    method,
    headers: requestHeaders,
  };

  if (body !== undefined) {
    requestHeaders.set("Content-Type", "application/json");
    options.body = JSON.stringify(body);
  }
  if (method === "GET") options.cache = "no-store";

  let response;
  try {
    response = await fetch(
      `${CONFIG.API_BASE}${path}${url ? `?${url}` : ""}`,
      options,
    );
  } catch (error) {
    return {
      ok: false,
      status: 0,
      data: { error: "Network error. Please check your connection." },
      headers: {},
    };
  }

  let data = null;
  try {
    const text = await response.text();
    if (text) data = JSON.parse(text);
  } catch (error) {
    data = null;
  }

  const responseHeaders = {};
  response.headers.forEach((value, key) => {
    responseHeaders[key.toLowerCase()] = value;
  });

  return {
    ok: response.ok,
    status: response.status,
    data,
    headers: responseHeaders,
  };
}

/**
 * Get available appointment slots, optionally filtered by provider name.
 * @param {string} [provider] - partial provider name (case-insensitive)
 * @returns {Promise<{ok, status, data, headers}>}
 */
async function getSlots(provider) {
  const query = {};
  if (provider && provider.trim()) query.provider = provider.trim();
  return request("GET", "/slots", { query });
}

/**
 * Create a booking.
 * @param {{slot_id: string, name: string, email: string}} payload
 * @param {string} idempotencyKey - UUID sent as Idempotency-Key header
 * @returns {Promise<{ok, status, data, headers}>}
 */
async function createBooking(payload, idempotencyKey) {
  const headers = {};
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return request("POST", "/bookings", { body: payload, headers });
}

/**
 * List bookings for an email, paginated.
 * @param {string} email
 * @param {number} limit - items per page
 * @param {string|null} cursor - opaque cursor for the next page
 * @returns {Promise<{ok, status, data, headers}>}
 */
async function listBookings(email, limit = 5, cursor = null) {
  const query = { email, limit };
  if (cursor !== null && cursor !== undefined && cursor !== "")
    query.cursor = cursor;
  return request("GET", "/bookings", { query });
}

/**
 * Cancel a booking by ID.
 * @param {string} id - booking ID
 * @returns {Promise<{ok, status, data, headers}>}
 */
async function cancelBooking(id) {
  return request("DELETE", `/bookings/${encodeURIComponent(id)}`);
}

// ----------------------------------------------------------
// 3. UI LAYER
// ----------------------------------------------------------

/** Build a lookup map from slot_id → slot object */
let slotLookup = {};
function buildSlotLookup(slots) {
  const nextLookup = {};
  slots.forEach((s) => {
    nextLookup[s.id] = s;
  });
  slotLookup = nextLookup;
}

async function refreshSlotLookup() {
  const result = await getSlots();
  if (result.ok && Array.isArray(result.data?.slots)) {
    buildSlotLookup(result.data.slots);
  }
}

// --- DOM references ---
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

// Tabs
const tabButtons = $$('[role="tab"]');
const tabAppointments = $("#panel-appointments");
const tabBookings = $("#panel-bookings");

// Theme
const themeToggle = $("#theme-toggle");

// Appointments
const searchInput = $("#search-input");
const searchClear = $("#search-clear");
const slotsContainer = $("#slots-container");

// Booking modal
const bookingBackdrop = $("#booking-backdrop");
const bookingModal = $("#booking-modal");
const bookingForm = $("#booking-form-view");
const confirmView = $("#booking-confirm-view");
const modalProvider = $("#modal-provider");
const modalDate = $("#modal-date");
const modalTime = $("#modal-time");
const inputName = $("#input-name");
const inputEmail = $("#input-email");
const btnConfirmBook = $("#btn-confirm-booking");
const btnCancelModal = $("#btn-cancel-modal");
const btnModalClose = $("#btn-modal-close");

// Confirmation
const confirmProvider = $("#confirm-provider");
const confirmDate = $("#confirm-date");
const confirmTime = $("#confirm-time");
const confirmName = $("#confirm-name");
const confirmEmail = $("#confirm-email");
const confirmStatus = $("#confirm-status");
const btnDone = $("#btn-done");
const btnViewBookings = $("#btn-view-bookings");

// Cancel modal
const cancelBackdrop = $("#cancel-backdrop");
const cancelModal = $("#cancel-modal");
const btnKeep = $("#btn-keep");
const btnYesCancel = $("#btn-yes-cancel");

// My Bookings
const bookingsEmail = $("#bookings-email");
const btnFindBookings = $("#btn-find-bookings");
const bookingsContainer = $("#bookings-container");

// Toast
const toastContainer = $("#toast-container");

// --- State ---
let currentSlotForBooking = null;
let idempotencyKey = null;
let searchDebounceTimer = null;
let lastBookedEmail = null;
let currentBookingsCursor = null;
let cancelTargetBookingId = null;

// ----------------------------------------------------------
// 3a. Tab switching
// ----------------------------------------------------------
function switchTab(tabId) {
  tabButtons.forEach((btn) => {
    const selected = btn.getAttribute("data-tab") === tabId;
    btn.setAttribute("aria-selected", String(selected));
    btn.tabIndex = selected ? 0 : -1;
  });

  tabAppointments.classList.toggle("active", tabId === "appointments");
  tabBookings.classList.toggle("active", tabId === "bookings");
  if (tabId === "bookings") refreshSlotLookup();
}

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
  btn.addEventListener("keydown", (e) => {
    const tabs = tabButtons;
    const idx = tabs.indexOf(btn);
    let next = -1;
    if (e.key === "ArrowRight") next = (idx + 1) % tabs.length;
    if (e.key === "ArrowLeft") next = (idx - 1 + tabs.length) % tabs.length;
    if (next >= 0) {
      e.preventDefault();
      tabs[next].focus();
      tabs[next].click();
    }
  });
});

// ----------------------------------------------------------
// 3b. Time formatting helpers
// ----------------------------------------------------------
const dateFormatter = new Intl.DateTimeFormat(undefined, {
  weekday: "long",
  day: "numeric",
  month: "long",
});

const timeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
});

function formatDateHeading(isoString) {
  return dateFormatter.format(new Date(isoString));
}

function formatTimeRange(startISO, endISO) {
  return `${timeFormatter.format(new Date(startISO))} – ${timeFormatter.format(new Date(endISO))}`;
}

function formatDate(isoString) {
  return dateFormatter.format(new Date(isoString));
}

/** Group key: YYYY-MM-DD in local timezone */
function dateKey(isoString) {
  const d = new Date(isoString);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// ----------------------------------------------------------
// 3c. Appointments tab
// ----------------------------------------------------------

/** Render skeleton loading cards */
function renderSlotSkeletons() {
  slotsContainer.innerHTML = Array.from(
    { length: 4 },
    () => `
    <div class="skeleton-card">
      <div class="skeleton-lines">
        <div class="skeleton-bar w-40"></div>
        <div class="skeleton-bar w-24"></div>
      </div>
      <div class="skeleton-bar w-16"></div>
    </div>
  `,
  ).join("");
}

/** Render the empty state */
function renderSlotsEmpty() {
  slotsContainer.innerHTML = `
    <div class="state-message">
      <p>No appointments found</p>
    </div>
  `;
}

/** Render an error state */
function renderSlotsError() {
  slotsContainer.innerHTML = `
    <div class="state-message">
      <p>Something went wrong loading appointments.</p>
      <button class="btn btn-secondary" id="btn-retry-slots">Try again</button>
    </div>
  `;
  $("#btn-retry-slots").addEventListener("click", loadSlots);
}

/** Determine the button state for a slot */
function slotButtonState(slot) {
  const now = new Date();
  if (new Date(slot.start) < now) {
    return { label: "Started", cls: "btn btn-faded", disabled: true };
  }
  if (!slot.available) {
    return { label: "Booked", cls: "btn btn-faded", disabled: true };
  }
  return { label: "Book", cls: "btn btn-primary", disabled: false };
}

/** Load and render slots */
async function loadSlots(refreshLookup = false) {
  renderSlotSkeletons();
  const provider = searchInput.value.trim();
  if (refreshLookup && provider) refreshSlotLookup();
  const result = await getSlots(provider || undefined);

  if (!result.ok) {
    renderSlotsError();
    showToast(
      "error",
      result.data?.error || "Failed to load appointments.",
      result.headers,
      result.status,
    );
    return;
  }

  const slots = result.data.slots;
  if (!provider) buildSlotLookup(slots);

  if (slots.length === 0) {
    renderSlotsEmpty();
    return;
  }

  // Group by date
  const groups = new Map();
  slots.forEach((slot) => {
    const key = dateKey(slot.start);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(slot);
  });

  // Sort groups chronologically
  const sortedKeys = [...groups.keys()].sort();

  let html = "";
  sortedKeys.forEach((key) => {
    const group = groups.get(key);
    html += `<h3 class="date-heading">${formatDateHeading(group[0].start)}</h3>`;
    group.forEach((slot) => {
      const btn = slotButtonState(slot);
      html += `
        <div class="slot-card">
          <div class="slot-info">
            <span class="slot-time">${formatTimeRange(slot.start, slot.end)}</span>
            <span class="slot-provider">${escapeHtml(slot.provider)}</span>
          </div>
          <button
            class="${btn.cls}"
            ${btn.disabled ? "disabled" : ""}
            ${!btn.disabled ? `data-slot-id="${slot.id}"` : ""}
          >${btn.label}</button>
        </div>
      `;
    });
  });

  slotsContainer.innerHTML = html;

  // Attach book click handlers
  $$("[data-slot-id]", slotsContainer).forEach((btn) => {
    btn.addEventListener("click", () =>
      openBookingModal(btn.getAttribute("data-slot-id")),
    );
  });
}

/** Simple HTML escape */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// --- Search ---
searchInput.addEventListener("input", () => {
  searchClear.classList.toggle("visible", searchInput.value.length > 0);
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(loadSlots, 350);
});

searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    clearTimeout(searchDebounceTimer);
    loadSlots();
  }
});

searchClear.addEventListener("click", () => {
  searchInput.value = "";
  searchClear.classList.remove("visible");
  loadSlots(true);
});

// ----------------------------------------------------------
// 3d. Booking modal
// ----------------------------------------------------------
function generateUUID() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function openBookingModal(slotId) {
  const slot = slotLookup[slotId];
  if (!slot) return;

  currentSlotForBooking = slot;
  idempotencyKey = generateUUID();

  // Populate summary
  modalProvider.textContent = slot.provider;
  modalDate.textContent = formatDate(slot.start);
  modalTime.textContent = formatTimeRange(slot.start, slot.end);

  // Reset form
  inputName.value = "";
  inputEmail.value = "";
  btnConfirmBook.disabled = false;
  btnConfirmBook.innerHTML = "Confirm booking";

  // Show form, hide confirmation
  bookingForm.style.display = "";
  confirmView.style.display = "none";

  openModal(bookingBackdrop);
}

function closeBookingModal() {
  closeModal(bookingBackdrop);
  currentSlotForBooking = null;
  idempotencyKey = null;
}

btnModalClose.addEventListener("click", closeBookingModal);
btnCancelModal.addEventListener("click", closeBookingModal);

// Submit booking
btnConfirmBook.addEventListener("click", async () => {
  const name = inputName.value;
  const email = inputEmail.value;

  // Disable button and show spinner
  btnConfirmBook.disabled = true;
  btnConfirmBook.innerHTML = '<span class="spinner"></span>';

  const result = await createBooking(
    { slot_id: currentSlotForBooking.id, name, email },
    idempotencyKey,
  );

  if (!result.ok) {
    btnConfirmBook.disabled = false;
    btnConfirmBook.innerHTML = "Confirm booking";
    showToast(
      "error",
      result.data?.error || "Booking failed.",
      result.headers,
      result.status,
    );
    return;
  }

  // Success — show confirmation view
  const slot = currentSlotForBooking;
  const booking = result.data;

  confirmProvider.textContent = slot.provider;
  confirmDate.textContent = formatDate(slot.start);
  confirmTime.textContent = formatTimeRange(slot.start, slot.end);
  confirmName.textContent = booking.name;
  confirmEmail.textContent = booking.email;
  confirmStatus.textContent = booking.status;

  lastBookedEmail = booking.email;

  bookingForm.style.display = "none";
  confirmView.style.display = "";

  showToast("success", "Your appointment is booked");

  // Refresh slot list in background
  loadSlots(true);
});

btnDone.addEventListener("click", closeBookingModal);

btnViewBookings.addEventListener("click", () => {
  closeBookingModal();
  switchTab("bookings");
  if (lastBookedEmail) {
    bookingsEmail.value = lastBookedEmail;
    loadBookings(lastBookedEmail);
  }
});

// ----------------------------------------------------------
// 3e. Generic modal helpers (open / close / focus trap / esc)
// ----------------------------------------------------------
let focusTrapStack = [];

function openModal(backdrop) {
  const modal = $(".modal", backdrop);
  backdrop.classList.add("open");

  // Focus trap
  const focusableEls = $$(
    'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    modal,
  );
  const trap = {
    backdrop,
    first: focusableEls[0],
    last: focusableEls[focusableEls.length - 1],
  };
  focusTrapStack.push(trap);

  if (trap.first) trap.first.focus();

  backdrop._keyHandler = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      closeModal(backdrop);
      return;
    }
    if (e.key === "Tab" && focusableEls.length > 0) {
      if (e.shiftKey && document.activeElement === trap.first) {
        e.preventDefault();
        trap.last.focus();
      } else if (!e.shiftKey && document.activeElement === trap.last) {
        e.preventDefault();
        trap.first.focus();
      }
    }
  };
  document.addEventListener("keydown", backdrop._keyHandler);

  backdrop._clickHandler = (e) => {
    if (e.target === backdrop) closeModal(backdrop);
  };
  backdrop.addEventListener("click", backdrop._clickHandler);
}

function closeModal(backdrop) {
  backdrop.classList.remove("open");
  if (backdrop._keyHandler) {
    document.removeEventListener("keydown", backdrop._keyHandler);
    backdrop._keyHandler = null;
  }
  if (backdrop._clickHandler) {
    backdrop.removeEventListener("click", backdrop._clickHandler);
    backdrop._clickHandler = null;
  }
  focusTrapStack = focusTrapStack.filter((t) => t.backdrop !== backdrop);
}

// ----------------------------------------------------------
// 3f. My Bookings tab
// ----------------------------------------------------------

function renderBookingSkeletons() {
  bookingsContainer.innerHTML = Array.from(
    { length: 3 },
    () => `
    <div class="skeleton-card">
      <div class="skeleton-lines">
        <div class="skeleton-bar w-40"></div>
        <div class="skeleton-bar w-24"></div>
      </div>
      <div class="skeleton-bar w-16"></div>
    </div>
  `,
  ).join("");
}

function renderBookingsEmpty() {
  bookingsContainer.innerHTML = `
    <div class="state-message">
      <p>No bookings found for this email</p>
    </div>
  `;
}

function renderBookingsError(msg) {
  bookingsContainer.innerHTML = `
    <div class="state-message">
      <p>${escapeHtml(msg || "Something went wrong loading bookings.")}</p>
      <button class="btn btn-secondary" id="btn-retry-bookings">Try again</button>
    </div>
  `;
  $("#btn-retry-bookings").addEventListener("click", () => {
    loadBookings(bookingsEmail.value.trim());
  });
}

/** Render booking cards and optional pagination button */
function renderBookings(bookings, nextCursor) {
  if (bookings.length === 0) {
    renderBookingsEmpty();
    return;
  }

  let html = "";
  bookings.forEach((booking) => {
    const slot = slotLookup[booking.slot_id];
    const provider = slot ? slot.provider : "Unknown provider";
    const time = slot ? formatTimeRange(slot.start, slot.end) : "";
    const date = slot ? formatDate(slot.start) : "";

    const isConfirmed = booking.status === "confirmed";
    const badgeCls = isConfirmed
      ? "badge badge-confirmed"
      : "badge badge-cancelled";
    const badgeLabel = isConfirmed ? "Confirmed" : "Cancelled";

    html += `
      <div class="booking-card" data-booking-id="${booking.id}">
        <div class="booking-info">
          <span class="booking-provider">${escapeHtml(provider)}</span>
          <span class="booking-time">${date ? `${date}, ${time}` : ""}</span>
          <span class="booking-name">${escapeHtml(booking.name)}</span>
        </div>
        <div class="booking-actions">
          <span class="${badgeCls}">${badgeLabel}</span>
          ${isConfirmed ? `<button class="btn btn-secondary btn-cancel-booking" data-booking-id="${booking.id}">Cancel</button>` : ""}
        </div>
      </div>
    `;
  });

  // Pagination — circular chevron icon button
  if (nextCursor) {
    html += `
      <div class="pagination-wrapper">
        <button class="btn-paginate" id="btn-next-page" data-cursor="${nextCursor}" aria-label="Next page" title="Next page">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="9 18 15 12 9 6"></polyline>
          </svg>
        </button>
      </div>
    `;
  }

  bookingsContainer.innerHTML = html;

  // Cancel button handlers
  $$(".btn-cancel-booking", bookingsContainer).forEach((btn) => {
    btn.addEventListener("click", () =>
      openCancelModal(btn.getAttribute("data-booking-id")),
    );
  });

  // Pagination handler
  const btnNext = $("#btn-next-page", bookingsContainer);
  if (btnNext) {
    btnNext.addEventListener("click", () => {
      const cursor = btnNext.getAttribute("data-cursor");
      loadBookings(bookingsEmail.value.trim(), cursor);
    });
  }
}

async function loadBookings(email, cursor = null) {
  if (!email) return;

  renderBookingSkeletons();
  const result = await listBookings(email, 5, cursor);

  if (!result.ok) {
    renderBookingsError(result.data?.error);
    showToast(
      "error",
      result.data?.error || "Failed to load bookings.",
      result.headers,
      result.status,
    );
    return;
  }

  currentBookingsCursor = result.data.next_cursor;
  renderBookings(result.data.bookings, result.data.next_cursor);
  showToast("success", "Bookings loaded");
}

btnFindBookings.addEventListener("click", () => {
  refreshSlotLookup();
  loadBookings(bookingsEmail.value.trim());
});

bookingsEmail.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    refreshSlotLookup();
    loadBookings(bookingsEmail.value.trim());
  }
});

// ----------------------------------------------------------
// 3g. Cancel booking flow
// ----------------------------------------------------------
function openCancelModal(bookingId) {
  cancelTargetBookingId = bookingId;
  openModal(cancelBackdrop);
}

function closeCancelModal() {
  closeModal(cancelBackdrop);
  cancelTargetBookingId = null;
}

btnKeep.addEventListener("click", closeCancelModal);

btnYesCancel.addEventListener("click", async () => {
  if (!cancelTargetBookingId) return;

  btnYesCancel.disabled = true;
  btnYesCancel.innerHTML = '<span class="spinner"></span>';

  const result = await cancelBooking(cancelTargetBookingId);

  btnYesCancel.disabled = false;
  btnYesCancel.innerHTML = "Yes, cancel";

  closeCancelModal();

  if (!result.ok) {
    showToast(
      "error",
      result.data?.error || "Failed to cancel booking.",
      result.headers,
      result.status,
    );
    return;
  }

  showToast("success", "Appointment cancelled");

  // Update the specific card in-place
  const card = $(`[data-booking-id="${result.data.id}"].booking-card`);
  if (card) {
    const actionsEl = $(".booking-actions", card);
    actionsEl.innerHTML =
      '<span class="badge badge-cancelled">Cancelled</span>';
  }

  // Refresh slots
  loadSlots(true);
});

// ----------------------------------------------------------
// 3h. Theme (light / dark)
// ----------------------------------------------------------
const THEME_KEY = "clinic-theme";
const systemThemeQuery = window.matchMedia
  ? window.matchMedia("(prefers-color-scheme: dark)")
  : null;

let themeIsPinned = false; // true once the user has chosen manually
let themeTransitionTimer = null;

/** Read the saved preference; never throw when storage is blocked. */
function readSavedTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return saved === "light" || saved === "dark" ? saved : null;
  } catch (e) {
    return null;
  }
}

/** Persist the preference; a failure must not break the toggle. */
function saveTheme(theme) {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch (e) {
    /* Storage unavailable — the theme still applies for this session. */
  }
}

function getTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark"
    ? "dark"
    : "light";
}

function updateThemeToggle(theme) {
  if (!themeToggle) return;
  const label =
    theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
  themeToggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
  themeToggle.setAttribute("aria-label", label);
  themeToggle.setAttribute("title", label);
}

/** Apply a theme; animate only for user or system switches, never on load. */
function applyTheme(theme, animate) {
  const root = document.documentElement;

  if (animate) {
    root.classList.add("theme-transitioning");
    clearTimeout(themeTransitionTimer);
    themeTransitionTimer = setTimeout(() => {
      root.classList.remove("theme-transitioning");
    }, 250);
  }

  root.setAttribute("data-theme", theme);
  updateThemeToggle(theme);
}

function initTheme() {
  themeIsPinned = readSavedTheme() !== null;
  applyTheme(getTheme(), false);

  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const next = getTheme() === "dark" ? "light" : "dark";
      themeIsPinned = true;
      saveTheme(next);
      applyTheme(next, true);
    });
  }

  if (systemThemeQuery) {
    const onSystemThemeChange = (event) => {
      if (themeIsPinned) return;
      applyTheme(event.matches ? "dark" : "light", true);
    };
    if (systemThemeQuery.addEventListener) {
      systemThemeQuery.addEventListener("change", onSystemThemeChange);
    } else if (systemThemeQuery.addListener) {
      systemThemeQuery.addListener(onSystemThemeChange);
    }
  }
}

// ----------------------------------------------------------
// 4. TOAST SYSTEM
// ----------------------------------------------------------

/** Friendly message map for common server errors */
const FRIENDLY_MESSAGES = {
  "Failed to fetch": "Could not reach the server. Check your connection.",
  NetworkError: "Network error. Please check your connection.",
};

/**
 * Show a toast notification.
 * @param {'success'|'error'} type
 * @param {string} message
 * @param {object} [headers] - response headers (for Retry-After)
 * @param {number} [status] - response status
 */
function showToast(type, message, headers, status) {
  // Handle HTTP 429 Retry-After
  const retryAfter = headers?.["retry-after"];
  if (
    status === 429 &&
    typeof retryAfter === "string" &&
    /^\d+$/.test(retryAfter)
  ) {
    const seconds = Number(retryAfter);
    if (Number.isInteger(seconds)) {
      const separator = /[.!?]$/.test(message.trim()) ? " " : ". ";
      message = `${message.trim()}${separator}Try again in ${seconds} ${seconds === 1 ? "second" : "seconds"}.`;
    }
  }

  // Map known error messages to friendly text
  if (type === "error" && FRIENDLY_MESSAGES[message]) {
    message = FRIENDLY_MESSAGES[message];
  }

  const iconSvg =
    type === "success"
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';

  const toast = document.createElement("div");
  toast.className = "toast";
  toast.innerHTML = `
    <div class="toast-icon ${type}">${iconSvg}</div>
    <span class="toast-message">${escapeHtml(message)}</span>
    <button class="toast-close" aria-label="Dismiss">&times;</button>
  `;

  toastContainer.appendChild(toast);

  const dismiss = () => {
    toast.classList.add("dismissing");
    setTimeout(() => toast.remove(), 200);
  };

  $(".toast-close", toast).addEventListener("click", dismiss);

  // Auto-dismiss after ~4 seconds
  setTimeout(dismiss, 4000);
}

// ----------------------------------------------------------
// 5. INIT
// ----------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  switchTab("appointments");
  loadSlots();
});
