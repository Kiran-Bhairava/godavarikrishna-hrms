const API = "/api";
      let token = localStorage.getItem("token");
      let user = null;
      let currentFilter = "all";
      let allRows = []; // attendance rows
      let obAllRows = []; // onboarding employee rows (from API)
      let obFilter = "all";
      let currentStep = 1;
      let branchList = [];
      let managerList = []; // all active employees eligible as L1/L2

      const COLORS = [
        "#3b63f6",
        "#10b981",
        "#f59e0b",
        "#ef4444",
        "#8b5cf6",
        "#06b6d4",
        "#ec4899",
      ];
      let colorMap = {},
        colorIdx = 0;
      function getColor(n) {
        if (!colorMap[n]) colorMap[n] = COLORS[colorIdx++ % COLORS.length];
        return colorMap[n];
      }

      function toast(msg, type = "success") {
        const el = document.getElementById("toast");
        el.textContent = msg;
        el.className = `toast ${type} show`;
        setTimeout(() => el.classList.remove("show"), 3500);
      }

      // ── API ──────────────────────────────────────────────────────
      async function apiFetch(url, method = "GET", body = null) {
        const opts = {
          method,
          headers: { "Content-Type": "application/json" },
        };
        if (token) opts.headers["Authorization"] = `Bearer ${token}`;
        if (body) opts.body = JSON.stringify(body);
        const res = await fetch(API + url, opts);

        // Token expired or invalid — kick to login
        if (res.status === 401) {
          localStorage.removeItem("token");
          window.location.href = "/login";
          throw new Error("Session expired. Please log in again.");
        }

        const data = await res.json();
        if (!res.ok) {
          // FastAPI returns detail as string for most errors,
          // but as [{msg, loc}] array for 422 validation errors.
          let msg = "Request failed";
          if (data.detail) {
            if (Array.isArray(data.detail)) {
              msg = data.detail.map((e) => e.msg || JSON.stringify(e)).join("; ");
            } else {
              msg = data.detail;
            }
          }
          throw new Error(msg);
        }
        return data;
      }

      async function checkAuth() {
        if (!token) {
          window.location.href = "/login";
          return false;
        }
        try {
          user = await apiFetch("/auth/me");
          return true;
        } catch (e) {
          localStorage.removeItem("token");
          window.location.href = "/login";
          return false;
        }
      }

      function logout() {
        token = null;
        user = null;
        localStorage.removeItem("token");
        window.location.href = "/login";
      }

      // ── View switching ────────────────────────────────────────────
      function switchView(name) {
        document
          .querySelectorAll(".view")
          .forEach((v) => v.classList.remove("active"));
        document.getElementById(name + "View").classList.add("active");
        document
          .querySelectorAll(".nav-item")
          .forEach((n) => n.classList.remove("active"));
        const labels = {
          attendance: "Attendance Report",
          onboarding: "Onboarding",
          addEmployee: "Add New Employee",
          regAudit: "Regularizations",
          leaveApprovals: "Leave Approvals",
        };
        document.getElementById("topbarSection").textContent =
          labels[name] || name;
        if (name === "onboarding" || name === "addEmployee")
          document.getElementById("navOnboarding").classList.add("active");
        if (name === "attendance")
          document
            .querySelector(".nav-item[onclick=\"switchView('attendance')\"]")
            ?.classList.add("active");
        if (name === "regAudit")
          document.getElementById("navRegAudit")?.classList.add("active");
        if (name === "leaveApprovals")
          document.getElementById("navLeaveApprovals")?.classList.add("active");
        if (name === "addEmployee") {
          goToStep(1);
          populateBranchDropdown();
          populateManagerDropdowns();
        }
        if (name === "onboarding") loadOnboarding();
        if (name === "regAudit") loadRegAudit(1);
        if (name === "attendance") loadReport();
        if (name === "leaveApprovals") initLeaveApprovals();
        // Persist view so page refresh restores same section
        try { sessionStorage.setItem("hr_view", name); } catch(_) {}
        window.scrollTo(0, 0);
      }

      // ── Init ─────────────────────────────────────────────────────
      async function initHR() {
        const ok = await checkAuth();
        if (!ok) return;
        if (user.role !== "hr" && user.role !== "admin") {
          window.location.href = "/employee";
          return;
        }

        const ini = user.full_name[0].toUpperCase();
        ["sidebarAvatar", "topbarAvatar"].forEach(
          (id) => (document.getElementById(id).textContent = ini),
        );
        ["sidebarName", "topbarName"].forEach(
          (id) => (document.getElementById(id).textContent = user.full_name),
        );

        document.getElementById("reportDate").value = new Date()
          .toISOString()
          .split("T")[0];

        // ── Restore view FIRST, before any slow data fetches ────────
        // This eliminates the flash: onboarding → target view.
        // All views are hidden in HTML; JS reveals the right one immediately.
        const VALID_VIEWS = ["attendance", "onboarding", "regAudit", "leaveApprovals"];
        let savedView = "onboarding";
        try { savedView = sessionStorage.getItem("hr_view") || "onboarding"; } catch(_) {}
        if (!VALID_VIEWS.includes(savedView)) savedView = "onboarding";
        switchView(savedView);

        // ── Load data in background after view is already shown ─────
        await Promise.all([loadBranches(), loadManagers()]);
        // Only pre-fetch attendance report data if that view is active,
        // otherwise it loads on demand when user navigates to it.
        if (savedView === "attendance") await loadReport();
      }

      // ── Branches ─────────────────────────────────────────────────
      async function loadBranches() {
        try {
          branchList = await apiFetch("/hr/branches");
          const sel = document.getElementById("branchFilter");
          sel.innerHTML =
            '<option value="">All Branches</option>' +
            branchList
              .map(
                (b) => `<option value="${b.id}">${b.name} · ${b.city}</option>`,
              )
              .join("");
        } catch (e) {
          toast("Failed to load branches", "error");
        }
      }

      function populateBranchDropdown() {
        const sel = document.getElementById("f_branch");
        sel.innerHTML =
          '<option value="">Select Branch</option>' +
          branchList
            .map(
              (b) => `<option value="${b.id}">${b.name} · ${b.city}</option>`,
            )
            .join("");
      }

      // ── Managers (L1 / L2) ───────────────────────────────────────
      async function loadManagers() {
        try {
          managerList = await apiFetch("/hr/managers");
        } catch (e) {
          console.warn("Could not load managers:", e.message);
        }
      }

      function populateManagerDropdowns() {
        const opts =
          '<option value="">Select</option>' +
          managerList
            .map((m) => {
              const role = m.role.charAt(0).toUpperCase() + m.role.slice(1);
              const title = m.job_title ? ` | ${m.job_title}` : "";
              const dept = m.department ? ` (${m.department})` : "";
              return `<option value="${m.id}">${m.full_name}${title}${dept} | ${role}</option>`;
            })
            .join("");
        document.getElementById("f_l1_manager").innerHTML = opts;
        document.getElementById("f_l2_manager").innerHTML = opts;
      }

      // ── Attendance report ─────────────────────────────────────────
      function formatTime(dt) {
        if (!dt) return null;
        return new Date(dt).toLocaleTimeString("en-US", {
          hour: "2-digit",
          minute: "2-digit",
          hour12: true,
        });
      }
      function formatDate(dt) {
        const d = new Date(dt);
        return {
          date: d.toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
          }),
          day: d.toLocaleDateString("en-US", { weekday: "long" }),
        };
      }

      function setFilter(f, el) {
        currentFilter = f;
        document
          .querySelectorAll(".filter-tab")
          .forEach((t) => t.classList.remove("active"));
        el.classList.add("active");
        renderTable();
      }

      function renderTable(sq = "") {
        const tbody = document.getElementById("reportTable");
        let rows = allRows;
        if (currentFilter !== "all")
          rows = rows.filter((e) => {
            const s =
              e.status === "present"
                ? e.is_late
                  ? "late"
                  : "present"
                : "absent";
            return s === currentFilter;
          });
        if (sq)
          rows = rows.filter(
            (e) =>
              e.full_name.toLowerCase().includes(sq) ||
              (e.email || "").toLowerCase().includes(sq) ||
              (e.branch_name || "").toLowerCase().includes(sq),
          );
        document.getElementById("showingCount").textContent = rows.length;
        document.getElementById("totalCount").textContent = allRows.length;
        if (!rows.length) {
          tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No entries match the current filters.</td></tr>`;
          return;
        }
        const d = formatDate(
          document.getElementById("reportDate").value + "T12:00:00",
        );
        tbody.innerHTML = rows
          .map((e) => {
            const status =
              e.status === "present"
                ? e.is_late
                  ? "late"
                  : "present"
                : "absent";
            const sLabel = {
              present: "Present",
              late: "Late",
              absent: "Absent",
            };
            const hrs = e.total_minutes
              ? `${Math.floor(e.total_minutes / 60)}h ${e.total_minutes % 60}m`
              : "0h 00m";
            const tIn = formatTime(e.first_punch_in),
              tOut = formatTime(e.last_punch_out);
            const bg = getColor(e.full_name);
            const ini = e.full_name
              .split(" ")
              .map((n) => n[0])
              .join("")
              .slice(0, 2)
              .toUpperCase();
            return `<tr>
        <td><div style="font-weight:600;font-size:13.5px">${d.date}</div><div style="font-size:11px;color:var(--text-muted)">${d.day}</div></td>
        <td><div class="emp-cell"><div class="emp-avatar" style="background:${bg}">${ini}</div><div><div class="emp-name">${e.full_name}</div><div class="emp-role">${(e.email || "").split("@")[0]}</div></div></div></td>
        <td>${tIn ? `<span class="time-in"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>${tIn}</span>` : '<span style="color:var(--text-muted)">—</span>'}</td>
        <td>${tOut ? `<span class="time-out"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>${tOut}</span>` : '<span style="color:var(--text-muted)">—</span>'}</td>
        <td><span class="hours-val">${hrs}</span></td>
        <td><span class="badge ${status}"><span class="badge-dot"></span>${sLabel[status]}</span></td>
        <td><button class="action-btn"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg></button></td>
      </tr>`;
          })
          .join("");
      }

      async function loadReport() {
        const date = document.getElementById("reportDate").value;
        const branchId = document.getElementById("branchFilter").value;
        document.getElementById("reportTable").innerHTML =
          `<tr><td colspan="7" class="empty-state">Loading…</td></tr>`;
        try {
          let url = `/hr/daily-report?date_str=${date}`;
          if (branchId) url += `&branch_id=${branchId}`;
          const data = await apiFetch(url);
          const { total, present, absent, late } = data.stats;
          document.getElementById("kpiTotal").textContent = total;
          document.getElementById("kpiPresent").textContent = present;
          document.getElementById("kpiAbsent").textContent = absent;
          document.getElementById("kpiLate").textContent = late;
          if (total > 0) {
            document.getElementById("presentPercentage").textContent =
              `${Math.round((present / total) * 100)}% attendance`;
            document.getElementById("absentPercentage").textContent =
              `${Math.round((absent / total) * 100)}% absent`;
            document.getElementById("latePercentage").textContent =
              `${Math.round((late / total) * 100)}% late`;
          }
          const totalMins = (data.employees || []).reduce(
            (s, e) => s + (e.total_minutes || 0),
            0,
          );
          document.getElementById("totalHours").textContent = Math.round(
            totalMins / 60,
          ).toLocaleString();
          document.getElementById("activeStaff").textContent = present;
          document.getElementById("staffFraction").textContent =
            `${present} / ${total}`;
          allRows = data.employees || [];
          document.getElementById("totalCount").textContent = allRows.length;
          renderTable();
        } catch (e) {
          document.getElementById("reportTable").innerHTML =
            `<tr><td colspan="7" class="empty-state" style="color:var(--danger)">Error: ${e.message}</td></tr>`;
        }
      }

      async function exportExcel() {
        const date = document.getElementById("reportDate").value;
        const branchId = document.getElementById("branchFilter").value;
        toast("Downloading Excel file…", "success");
        try {
          let url = `${API}/hr/export?date_str=${date}`;
          if (branchId) url += `&branch_id=${branchId}`;
          const res = await fetch(url, {
            headers: { Authorization: `Bearer ${token}` },
          });
          if (!res.ok) throw new Error("Export failed");
          const blob = await res.blob();
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = `attendance_${date}.xlsx`;
          a.click();
          URL.revokeObjectURL(a.href);
          toast("Excel downloaded!", "success");
        } catch (e) {
          toast(e.message, "error");
        }
      }

      // ── Onboarding ────────────────────────────────────────────────
      async function loadOnboarding() {
        // Load KPI stats
        try {
          const stats = await apiFetch("/hr/onboarding-stats");
          const total = Number(stats.total) || 0;
          const inProg = Number(stats.in_progress) || 0;
          const done = Number(stats.completed) || 0;
          const waiting = Number(stats.awaiting) || 0;
          // Update KPI cards (use existing DOM positions)
          const kpis = document.querySelectorAll(".ob-kpi-val");
          if (kpis[0]) kpis[0].textContent = total;
          if (kpis[1]) kpis[1].textContent = inProg;
          if (kpis[2]) kpis[2].textContent = done;
          if (kpis[3]) kpis[3].textContent = waiting;
          // Badge in sidebar
          const badge = document.querySelector(".nav-badge");
          if (badge) badge.textContent = waiting;
        } catch (e) {
          console.warn("Stats error:", e.message);
        }

        // Load employee list
        await loadOnboardingTable();
      }

      async function loadOnboardingTable(statusFilter = "", search = "") {
        const tbody = document.getElementById("obTable");
        tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Loading…</td></tr>`;
        try {
          let url = "/hr/employees?";
          if (statusFilter && statusFilter !== "all")
            url += `onboarding_status=${statusFilter}&`;
          if (search) url += `search=${encodeURIComponent(search)}&`;
          const data = await apiFetch(url);
          obAllRows = data.employees || [];
          document.getElementById("obShowing").textContent = obAllRows.length;
          document.getElementById("obTotalCount").textContent =
            data.total || obAllRows.length;
          renderOnboardingTable();
        } catch (e) {
          tbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="color:var(--danger)">Error: ${e.message}</td></tr>`;
        }
      }

      function setObFilter(f, el) {
        obFilter = f;
        document
          .querySelectorAll(".ob-tab")
          .forEach((t) => t.classList.remove("active"));
        el.classList.add("active");
        loadOnboardingTable(f);
      }

      function filterOnboarding(q) {
        loadOnboardingTable(obFilter === "all" ? "" : obFilter, q);
      }

      function renderOnboardingTable() {
        const tbody = document.getElementById("obTable");
        const rows = obAllRows;
        if (!rows.length) {
          tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No employees match the filter.</td></tr>`;
          return;
        }

        const sMap = {
          "in-progress":
            '<span class="ob-status in-progress"><span class="ob-status-dot"></span>In Progress</span>',
          completed:
            '<span class="ob-status completed"><span class="ob-status-dot"></span>Completed</span>',
          awaiting:
            '<span class="ob-status awaiting"><span class="ob-status-dot"></span>Awaiting Login</span>',
        };

        tbody.innerHTML = rows
          .map((r) => {
            const isDeactivated = r.is_active === false;
            const name = r.full_name || "";
            const bg = getColor(name);
            const ini = name
              .split(" ")
              .map((n) => n[0])
              .join("")
              .slice(0, 2)
              .toUpperCase();
            const doj = r.date_of_joining
              ? new Date(r.date_of_joining).toLocaleDateString("en-US", {
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                })
              : "—";
            let statusHtml;
            if (isDeactivated) {
              statusHtml = `
                  <span class="ob-status not-started" style="background:#f3f4f6;color:#6b7280">
                    <span class="ob-status-dot" style="background:#6b7280"></span>
                    Deactivated
                  </span>`;
            } else {
              statusHtml =
                sMap[r.onboarding_status] ||
                `<span class="ob-status not-started"><span class="ob-status-dot"></span>${r.onboarding_status}</span>`;
            }
            const branch = r.branch_name
              ? `${r.branch_name}${r.branch_city ? ", " + r.branch_city : ""}`
              : "—";
            return `<tr ${isDeactivated ? 'style="opacity:0.6"' : ""}>
        <td><div class="emp-cell">
          <div class="emp-avatar" style="background:${bg}">${ini}</div>
          <div><div class="emp-name">${name}</div><div class="emp-role">${r.email || ""}</div></div>
        </div></td>
        <td>
          <div style="font-weight:600;font-size:13.5px">${r.job_title || r.role || "—"}</div>
          <div style="font-size:12px;color:var(--text-dim)">${r.department || "—"}</div>
        </td>
        <td style="font-size:13px;color:var(--text-dim)">${branch}</td>
        <td>${statusHtml}</td>
        <td style="font-size:13px;color:var(--text-dim)">${doj}</td>
        <td style="white-space:nowrap">
          ${
            isDeactivated
              ? `
                <button class="btn btn-secondary"
                  onclick="reactivateEmployee(${r.id}, '${r.full_name.replace(/'/g, "\\'")}')">
                  Reactivate
                </button>
              `
              : `
                <span style="display:inline-flex;align-items:center;gap:6px">
                  <select class="form-select"
                    style="padding:5px 28px 5px 8px;font-size:12px;border-radius:8px"
                    onchange="updateObStatus(${r.id}, this.value, this)">
                    <option value="awaiting"    ${r.onboarding_status === "awaiting" ? "selected" : ""}>Awaiting</option>
                    <option value="in-progress" ${r.onboarding_status === "in-progress" ? "selected" : ""}>In Progress</option>
                    <option value="completed"   ${r.onboarding_status === "completed" ? "selected" : ""}>Completed</option>
                  </select>

                  <button class="action-btn" title="Edit employee" onclick="openEditModal(${r.id})">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                    </svg>
                  </button>
                </span>
              `
          }
        </td>
      </tr>`;
          })
          .join("");
      }

      async function updateObStatus(empId, status, selectEl) {
        try {
          await apiFetch(`/hr/employees/${empId}/onboarding-status`, "PATCH", {
            status,
          });
          toast(`Status updated to "${status}"`, "success");
          // Refresh stats badge + row in place
          const row = obAllRows.find((r) => r.id === empId);
          if (row) row.onboarding_status = status;
          renderOnboardingTable();
          // Refresh KPI counts silently
          apiFetch("/hr/onboarding-stats")
            .then((stats) => {
              const kpis = document.querySelectorAll(".ob-kpi-val");
              if (kpis[3]) kpis[3].textContent = Number(stats.awaiting) || 0;
              const badge = document.querySelector(".nav-badge");
              if (badge) badge.textContent = Number(stats.awaiting) || 0;
            })
            .catch(() => {});
        } catch (e) {
          toast(e.message, "error");
          // Revert select
          selectEl.value =
            obAllRows.find((r) => r.id === empId)?.onboarding_status ||
            "awaiting";
        }
      }

      // ── Add Employee form ─────────────────────────────────────────
      function goToStep(step) {
        currentStep = step;
        for (let i = 1; i <= 5; i++) {
          document
            .getElementById(`aePanel${i}`)
            .classList.toggle("active", i === step);
          const nav = document.getElementById(`stepNav${i}`);
          const num = document.getElementById(`stepNum${i}`);
          nav.classList.remove("active", "done");
          if (i < step) {
            nav.classList.add("done");
            num.innerHTML = "&#10003;";
          } else if (i === step) {
            nav.classList.add("active");
            num.textContent = i;
          } else {
            num.textContent = i;
          }
        }
        window.scrollTo(0, 0);
      }

      function nextStep(from) {
        if (!validateStep(from)) return;
        if (from < 5) goToStep(from + 1);
      }
      function prevStep(from) {
        if (from > 1) goToStep(from - 1);
      }

      const REQUIRED = {
        1: [
          ["f_full_name", "Full Name"],
          ["f_work_email", "Work Email"],
          ["f_phone", "Mobile Number"],
        ],
        2: [
          ["f_job_title", "Job Title"],
          ["f_department", "Department"],
          ["f_doj", "Date of Joining"],
          ["f_branch", "Branch"],
        ],
        3: [["f_emp_type", "Employment Type"]],
        4: [
          ["f_shift_start", "Shift Start"],
          ["f_shift_end", "Shift End"],
        ],
        5: [],
      };

      function validateStep(step) {
        // Required field presence check
        for (const [id, label] of REQUIRED[step] || []) {
          const el = document.getElementById(id);
          if (!el || !el.value.trim()) {
            toast(`${label} is required`, "error");
            el && el.focus();
            return false;
          }
        }

        // Step 1 — format validations
        if (step === 1) {
          const email = val("f_work_email");
          if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            toast("Enter a valid work email address", "error");
            document.getElementById("f_work_email").focus();
            return false;
          }
          const phone = val("f_phone");
          if (phone && !/^\d{10}$/.test(phone.replace(/\s/g, ""))) {
            toast("Mobile number must be 10 digits", "error");
            document.getElementById("f_phone").focus();
            return false;
          }
          const personalEmail = val("f_personal_email");
          if (personalEmail && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(personalEmail)) {
            toast("Enter a valid personal email address", "error");
            document.getElementById("f_personal_email").focus();
            return false;
          }
        }

        // Step 2 — L1 ≠ L2 guard
        if (step === 2) {
          const l1 = parseInt(val("f_l1_manager")) || null;
          const l2 = parseInt(val("f_l2_manager")) || null;
          if (l1 && l2 && l1 === l2) {
            toast("L1 and L2 manager cannot be the same person", "error");
            return false;
          }
        }

        // Step 5 — bank/financial format validations
        if (step === 5) {
          const pan = val("f_pan").toUpperCase();
          if (pan && !/^[A-Z]{5}[0-9]{4}[A-Z]$/.test(pan)) {
            toast("PAN must be in format: ABCDE1234F", "error");
            document.getElementById("f_pan").focus();
            return false;
          }
          const ifsc = val("f_ifsc").toUpperCase();
          if (ifsc && !/^[A-Z]{4}0[A-Z0-9]{6}$/.test(ifsc)) {
            toast("IFSC must be in format: ABCD0123456", "error");
            document.getElementById("f_ifsc").focus();
            return false;
          }
          const account = val("f_account_no").replace(/\s/g, "");
          if (account && !/^\d{9,18}$/.test(account)) {
            toast("Bank account number must be 9–18 digits", "error");
            document.getElementById("f_account_no").focus();
            return false;
          }
        }

        return true;
      }

      function calcSalary() {
        const ctc = parseFloat(document.getElementById("f_ctc").value) || 0;
        const monthly = ctc / 12;
        document.getElementById("f_basic").value = monthly
          ? Math.round(monthly * 0.5)
          : "";
        document.getElementById("f_hra").value = monthly
          ? Math.round(monthly * 0.2)
          : "";
      }

      function handlePhoto(input) {
        if (input.files && input.files[0])
          toast("Photo selected: " + input.files[0].name, "success");
      }

      function saveDraft() {
        toast("Draft saved", "success");
      }

      function val(id) {
        const el = document.getElementById(id);
        return el ? el.value.trim() : "";
      }

      async function submitEnrollment() {
        for (let i = 1; i <= 5; i++) {
          if (!validateStep(i)) {
            goToStep(i);
            return;
          }
        }

        const l1 = parseInt(val("f_l1_manager")) || null;
        const l2 = parseInt(val("f_l2_manager")) || null;

        // Final L1 = L2 guard before submit
        if (l1 && l2 && l1 === l2) {
          toast("L1 and L2 manager cannot be the same person", "error");
          goToStep(2);
          return;
        }

        const submitBtns = document.querySelectorAll(
          "[onclick='submitEnrollment()']",
        );
        submitBtns.forEach((b) => {
          b.disabled = true;
          b.textContent = "Submitting…";
        });

        try {
          const payload = {
            // Step 1
            full_name: val("f_full_name"),
            work_email: val("f_work_email").toLowerCase(),
            password: val("f_password") || null,
            personal_email: val("f_personal_email") || null,
            phone: val("f_phone") || null,
            dob: val("f_dob") || null,
            gender: val("f_gender") || null,
            blood_group: val("f_blood") || null,
            nationality: val("f_nationality") || null,
            home_address: val("f_address") || null,
            emg_name: val("f_emg_name") || null,
            emg_phone: val("f_emg_phone") || null,
            emg_rel: val("f_emg_rel") || null,
            // Step 2
            job_title: val("f_job_title") || null,
            designation: val("f_designation") || null,
            department: val("f_department") || null,
            sub_department: val("f_sub_dept") || null,
            grade: val("f_grade") || null,
            date_of_joining: val("f_doj") || null,
            branch_id: parseInt(val("f_branch")) || null,
            role: val("f_role") || "employee",
            cost_centre: val("f_cost_centre") || null,
            l1_manager_id: l1,
            l2_manager_id: l2,
            // Step 3
            employment_type: val("f_emp_type") || null,
            contract_end: val("f_contract_end") || null,
            probation_end: val("f_prob_end") || null,
            notice_period: val("f_notice") || null,
            // Step 4
            shift_start: val("f_shift_start") || "09:45",
            shift_end: val("f_shift_end") || "18:30",
            work_mode: val("f_work_mode") || "On-Site",
            weekly_off: val("f_weekly_off") || "Saturday & Sunday",
            work_location: val("f_work_loc") || null,
            asset_id: val("f_asset_id") || null,
            // Step 5 — normalise PAN + IFSC to uppercase before sending
            annual_ctc: parseFloat(val("f_ctc")) || null,
            pay_frequency: val("f_pay_freq") || "Monthly",
            pf_enrolled: document.getElementById("f_pf")?.value === "true",
            esic_applicable: document.getElementById("f_esic")?.value === "true",
            bank_name: val("f_bank_name") || null,
            bank_account: val("f_account_no") || null,
            bank_ifsc: val("f_ifsc").toUpperCase() || null,
            pan_number: val("f_pan").toUpperCase() || null,
          };

          const result = await apiFetch("/hr/employees", "POST", payload);

          // Show credentials modal before navigating away
          openCredModal(result);
          toast("Employee onboarded successfully", "success");

          // Refresh manager list once, then navigate
          await loadManagers();
          setTimeout(() => switchView("onboarding"), 1800);
        } catch (e) {
          toast(e.message, "error");
          submitBtns.forEach((b) => {
            b.disabled = false;
            b.textContent = "Submit Enrollment";
          });
        }
      }

      // ── Edit Modal ────────────────────────────────────────────────
      let editingEmpId = null;

      function switchModalTab(name, el) {
        document
          .querySelectorAll(".modal-tab")
          .forEach((t) => t.classList.remove("active"));
        document
          .querySelectorAll(".modal-panel")
          .forEach((p) => p.classList.remove("active"));
        el.classList.add("active");
        document.getElementById("modalTab-" + name).classList.add("active");
      }

      function closeEditModal(e) {
        if (e && e.target !== document.getElementById("editModal")) return;
        document.getElementById("editModal").classList.remove("open");
        editingEmpId = null;
      }

      async function openEditModal(empId) {
        editingEmpId = empId;
        // Reset to first tab
        document.querySelectorAll(".modal-tab")[0].click();

        try {
          const emp = await apiFetch("/hr/employees/" + empId);
          document.getElementById("editModalSubtitle").textContent =
            emp.full_name + " · " + (emp.emp_id || "");

          // Populate fields
          const set = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.value = val ?? "";
          };

          set("e_full_name", emp.full_name);
          set("e_dob", emp.dob);
          set("e_gender", emp.gender);
          set("e_blood_group", emp.blood_group);
          set("e_phone", emp.phone);
          set("e_personal_email", emp.personal_email);
          set("e_nationality", emp.nationality);
          set("e_home_address", emp.home_address);
          set("e_emg_name", emp.emg_name);
          set("e_emg_phone", emp.emg_phone);
          set("e_emg_rel", emp.emg_rel);
          set("e_job_title", emp.job_title);
          set("e_designation", emp.designation);
          set("e_department", emp.department);
          set("e_sub_department", emp.sub_department);
          set("e_grade", emp.grade);
          set("e_date_of_joining", emp.date_of_joining);
          set("e_branch_id", emp.branch_id);
          set("e_role", emp.role);
          set("e_l1_manager_id", emp.l1_manager_id);
          set("e_l2_manager_id", emp.l2_manager_id);
          set("e_cost_centre", emp.cost_centre);
          set("e_employment_type", emp.employment_type);
          set("e_notice_period", emp.notice_period);
          set("e_contract_end", emp.contract_end);
          set("e_probation_end", emp.probation_end);
          set("e_shift_start", emp.shift_start);
          set("e_shift_end", emp.shift_end);
          set("e_work_mode", emp.work_mode);
          set("e_weekly_off", emp.weekly_off);
          set("e_work_location", emp.work_location);
          set("e_asset_id", emp.asset_id);
          set("e_annual_ctc", emp.annual_ctc);
          set("e_pay_frequency", emp.pay_frequency);
          set("e_pf_enrolled", emp.pf_enrolled ? "true" : "false");
          set("e_esic_applicable", emp.esic_applicable ? "true" : "false");
          set("e_bank_name", emp.bank_name);
          set("e_bank_account", emp.bank_account);
          set("e_bank_ifsc", emp.bank_ifsc);
          set("e_pan_number", emp.pan_number);

          // Populate branch and manager dropdowns in modal
          const bSel = document.getElementById("e_branch_id");
          bSel.innerHTML =
            '<option value="">Select Branch</option>' +
            branchList
              .map(
                (b) => `<option value="${b.id}">${b.name} · ${b.city}</option>`,
              )
              .join("");
          bSel.value = emp.branch_id || "";

          const mOpts =
            '<option value="">Select</option>' +
            managerList
              .map((m) => {
                const title = m.job_title ? ` · ${m.job_title}` : "";
                const dept = m.department ? ` (${m.department})` : "";
                const role = m.role.charAt(0).toUpperCase() + m.role.slice(1);
                return `<option value="${m.id}">${m.full_name}${title}${dept} [${role}]</option>`;
              })
              .join("");
          document.getElementById("e_l1_manager_id").innerHTML = mOpts;
          document.getElementById("e_l2_manager_id").innerHTML = mOpts;
          set("e_l1_manager_id", emp.l1_manager_id);
          set("e_l2_manager_id", emp.l2_manager_id);

          // Always reset deactivate button state when modal opens
          const deactBtn = document.getElementById("editDeactivateBtn");
          if (deactBtn) { deactBtn.disabled = false; deactBtn.textContent = "Deactivate"; }

          document.getElementById("editModal").classList.add("open");
        } catch (e) {
          toast("Failed to load employee: " + e.message, "error");
        }
      }

      async function saveEdit() {
        if (!editingEmpId) return;
        const v = (id) => {
          const el = document.getElementById(id);
          return el ? el.value.trim() : "";
        };
        const payload = {
          full_name: v("e_full_name") || null,
          dob: v("e_dob") || null,
          gender: v("e_gender") || null,
          blood_group: v("e_blood_group") || null,
          phone: v("e_phone") || null,
          personal_email: v("e_personal_email") || null,
          nationality: v("e_nationality") || null,
          home_address: v("e_home_address") || null,
          emg_name: v("e_emg_name") || null,
          emg_phone: v("e_emg_phone") || null,
          emg_rel: v("e_emg_rel") || null,
          job_title: v("e_job_title") || null,
          designation: v("e_designation") || null,
          department: v("e_department") || null,
          sub_department: v("e_sub_department") || null,
          grade: v("e_grade") || null,
          date_of_joining: v("e_date_of_joining") || null,
          branch_id: parseInt(v("e_branch_id")) || null,
          role: v("e_role") || null,
          l1_manager_id: parseInt(v("e_l1_manager_id")) || null,
          l2_manager_id: parseInt(v("e_l2_manager_id")) || null,
          cost_centre: v("e_cost_centre") || null,
          employment_type: v("e_employment_type") || null,
          notice_period: v("e_notice_period") || null,
          contract_end: v("e_contract_end") || null,
          probation_end: v("e_probation_end") || null,
          shift_start: v("e_shift_start") || null,
          shift_end: v("e_shift_end") || null,
          work_mode: v("e_work_mode") || null,
          weekly_off: v("e_weekly_off") || null,
          work_location: v("e_work_location") || null,
          asset_id: v("e_asset_id") || null,
          annual_ctc: parseFloat(v("e_annual_ctc")) || null,
          pay_frequency: v("e_pay_frequency") || null,
          pf_enrolled: document.getElementById("e_pf_enrolled")?.value === "true",
          esic_applicable: document.getElementById("e_esic_applicable")?.value === "true",
          bank_name: v("e_bank_name") || null,
          bank_account: v("e_bank_account") || null,
          bank_ifsc: v("e_bank_ifsc").toUpperCase() || null,
          pan_number: v("e_pan_number").toUpperCase() || null,
        };

        // Guard: same person cannot be both L1 and L2
        if (payload.l1_manager_id && payload.l2_manager_id &&
            payload.l1_manager_id === payload.l2_manager_id) {
          toast("L1 and L2 manager cannot be the same person", "error");
          return;
        }

        // Guard: employee cannot be their own manager
        if (payload.l1_manager_id && payload.l1_manager_id === editingEmpId) {
          toast("Employee cannot be their own L1 manager", "error");
          return;
        }
        if (payload.l2_manager_id && payload.l2_manager_id === editingEmpId) {
          toast("Employee cannot be their own L2 manager", "error");
          return;
        }

        // Format validations
        if (payload.phone && !/^\d{10}$/.test(payload.phone.replace(/\s/g, ""))) {
          toast("Mobile number must be 10 digits", "error");
          return;
        }
        if (payload.pan_number && !/^[A-Z]{5}[0-9]{4}[A-Z]$/.test(payload.pan_number)) {
          toast("PAN must be in format: ABCDE1234F", "error");
          return;
        }
        if (payload.bank_ifsc && !/^[A-Z]{4}0[A-Z0-9]{6}$/.test(payload.bank_ifsc)) {
          toast("IFSC must be in format: ABCD0123456", "error");
          return;
        }
        if (payload.bank_account && !/^\d{9,18}$/.test(payload.bank_account.replace(/\s/g, ""))) {
          toast("Bank account number must be 9–18 digits", "error");
          return;
        }

        const saveBtn = document.querySelector(".modal-footer .btn-primary");
        const origText = saveBtn?.textContent;
        if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Saving…"; }

        try {
          await apiFetch("/hr/employees/" + editingEmpId, "PUT", payload);
          toast("Employee updated successfully", "success");
          document.getElementById("editModal").classList.remove("open");
          editingEmpId = null;
          loadOnboarding();
          loadManagers();
        } catch (e) {
          toast("Update failed: " + e.message, "error");
        } finally {
          if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = origText; }
        }
      }

      async function deactivateFromModal() {
        if (!editingEmpId) return;
        const nameEl = document.getElementById("editModalSubtitle");
        const name = nameEl ? nameEl.textContent.split(" · ")[0] : "this employee";
        if (!confirm(`Deactivate "${name}"? They will immediately lose login access.`))
          return;
        const deactBtn = document.getElementById("editDeactivateBtn");
        const origText = deactBtn?.textContent?.trim();
        if (deactBtn) { deactBtn.disabled = true; deactBtn.textContent = "Deactivating…"; }
        try {
          await apiFetch(
            "/hr/employees/" + editingEmpId + "/deactivate",
            "PATCH",
          );
          toast(`${name} deactivated`, "success");
          document.getElementById("editModal").classList.remove("open");
          editingEmpId = null;
          loadOnboarding();
        } catch (e) {
          toast("Deactivation failed: " + e.message, "error");
          if (deactBtn) { deactBtn.disabled = false; deactBtn.textContent = origText; }
        }
      }

      async function reactivateEmployee(empId, empName) {
        if (!confirm(`Reactivate "${empName || 'this employee'}"? They will regain login access.`)) return;
        try {
          await apiFetch(`/hr/employees/${empId}/reactivate`, "PATCH");
          toast(`${empName || "Employee"} reactivated`, "success");
          loadOnboarding();
          loadManagers();
        } catch (e) {
          toast("Reactivation failed: " + e.message, "error");
        }
      }

      function openCredModal(data) {
        document.getElementById("cred_email").value = data.email || "";
        document.getElementById("cred_password").value =
          data.temporary_password || "";
        document.getElementById("cred_login_url").value = data.login_url || "";

        document.getElementById("credModal").classList.add("open");
      }

      function closeCredModal() {
        document.getElementById("credModal").classList.remove("open");
      }

      function copyCreds() {
        const text = `Email: ${document.getElementById("cred_email").value}
      Password: ${document.getElementById("cred_password").value}
      Login: ${document.getElementById("cred_login_url").value}`;

        navigator.clipboard.writeText(text);
        toast("Credentials copied!", "success");
      }

      document.addEventListener("DOMContentLoaded", initHR);

      const _origSwitchView = switchView;
      switchView = function (name) {
        _origSwitchView(name);
      };
      if ("serviceWorker" in navigator) {
        window.addEventListener("load", () => {
          navigator.serviceWorker.register("/sw.js");
        });
      }

      // ══════════════════════════════════════════════════════════════
      // REGULARIZATION AUDIT
      // ══════════════════════════════════════════════════════════════

      let auditCurrentPage = 1;
      let auditTotalPages = 1;
      const AUDIT_PAGE_SIZE = 50;

      function clearAuditFilters() {
        document.getElementById("auditFromDate").value = "";
        document.getElementById("auditToDate").value = "";
        document.getElementById("auditStatus").value = "";
        loadRegAudit(1);
      }

      async function loadRegAudit(page = 1) {
        auditCurrentPage = page;

        const fromDate = document.getElementById("auditFromDate").value;
        const toDate = document.getElementById("auditToDate").value;
        const statusVal = document.getElementById("auditStatus").value;

        const params = new URLSearchParams({
          page,
          page_size: AUDIT_PAGE_SIZE,
        });
        if (fromDate) params.set("from_date", fromDate);
        if (toDate) params.set("to_date", toDate);
        if (statusVal) params.set("final_status", statusVal);

        const loading = document.getElementById("auditLoading");
        const table = document.getElementById("auditTable");
        const empty = document.getElementById("auditEmpty");
        const paging = document.getElementById("auditPagination");

        loading.style.display = "flex";
        table.style.display = "none";
        empty.style.display = "none";
        paging.style.display = "none";

        try {
          const data = await apiFetch(`/hr/regularization-audit?${params}`);

          // Stats
          const reqs = data.requests || [];
          let approved = 0,
            rejected = 0,
            pending = 0;
          reqs.forEach((r) => {
            if (r.final_status === "approved") approved++;
            else if (r.final_status === "rejected") rejected++;
            else pending++;
          });
          document.getElementById("auditTotalCount").textContent =
            data.total ?? reqs.length;
          document.getElementById("auditApprovedCount").textContent = approved;
          document.getElementById("auditRejectedCount").textContent = rejected;
          document.getElementById("auditPendingCount").textContent = pending;

          loading.style.display = "none";

          if (!reqs.length) {
            empty.style.display = "block";
            return;
          }

          // ── Helpers ────────────────────────────────────────────
          const fmtMin = (m) => {
            if (m == null) return "—";
            const h = Math.floor(m / 60),
              mn = m % 60;
            return h ? `${h}h ${mn}m` : `${mn}m`;
          };
          const fmtDate = (s) => {
            if (!s) return "—";
            const [y, mo, d] = s.split("-");
            const M = [
              "Jan",
              "Feb",
              "Mar",
              "Apr",
              "May",
              "Jun",
              "Jul",
              "Aug",
              "Sep",
              "Oct",
              "Nov",
              "Dec",
            ];
            return `${parseInt(d)} ${M[parseInt(mo) - 1]} ${y}`;
          };
          const COLORS = [
            "#4f46e5",
            "#0891b2",
            "#059669",
            "#d97706",
            "#dc2626",
            "#7c3aed",
          ];
          const empColor = (n) =>
            COLORS[(n?.charCodeAt(0) || 65) % COLORS.length];

          // ── Render rows ────────────────────────────────────────
          const tbody = document.getElementById("auditTbody");
          tbody.innerHTML = reqs
            .map((r) => {
              const letter = (r.employee_name || "?")[0].toUpperCase();
              const color = empColor(r.employee_name);

              const statusBadge = `<span class="audit-status-badge ${r.final_status}">${r.final_status}</span>`;

              // L1 cell — badge + manager name below
              const l1Cell = r.l1_status
                ? `<span class="audit-status-badge ${r.l1_status}">${r.l1_status}</span>
                   <div style="font-size:11px;color:#9ca3af;margin-top:3px;">${r.l1_manager || ""}</div>`
                : `<span style="color:#d1d5db;">—</span>`;

              // L2 cell — "Not required" when null (no L2 assigned)
              const l2Cell = r.l2_status
                ? `<span class="audit-status-badge ${r.l2_status}">${r.l2_status}</span>
                   <div style="font-size:11px;color:#9ca3af;margin-top:3px;">${r.l2_manager || ""}</div>`
                : `<span style="color:#9ca3af;font-size:12px;font-style:italic;">Not required</span>`;

              // Hours before = first audit entry that has a snapshot
              const snap = r.audit_trail?.find((a) => a.minutes_before != null);
              const beforeMin = snap?.minutes_before ?? null;
              // Hours after = current state (only meaningful once decided)
              const afterMin =
                r.final_status !== "pending" ? r.current_total_minutes : null;

              const trailCount = (r.audit_trail || []).length;

              return `<tr>
              <td>
                <div class="emp-cell">
                  <div class="emp-avatar" style="background:${color};flex-shrink:0;">${letter}</div>
                  <div>
                    <div class="emp-name">${r.employee_name}</div>
                    <div class="emp-role">${[r.emp_id, r.department].filter(Boolean).join(" · ")}</div>
                  </div>
                </div>
              </td>
              <td style="white-space:nowrap;font-size:13px;">${fmtDate(r.work_date)}</td>
              <td><span class="hours-val">${r.actual_worked}</span></td>
              <td><span class="hours-val">${r.requested}</span></td>
              <td>${l1Cell}</td>
              <td>${l2Cell}</td>
              <td>${statusBadge}</td>
              <td style="font-weight:600;color:${afterMin != null ? "#15803d" : "#9ca3af"};">${fmtMin(afterMin)}</td>
              <td>
                <button class="trail-btn" onclick='openAuditDrawer(${JSON.stringify(r).replace(/'/g, "&#39;")})'>
                  ${trailCount} action${trailCount !== 1 ? "s" : ""}
                </button>
              </td>
            </tr>`;
            })
            .join("");

          table.style.display = "table";

          // Pagination
          const total = data.total || reqs.length;
          auditTotalPages = Math.ceil(total / AUDIT_PAGE_SIZE);
          const start = (page - 1) * AUDIT_PAGE_SIZE + 1;
          const end = Math.min(page * AUDIT_PAGE_SIZE, total);
          document.getElementById("auditPageInfo").textContent =
            `${start}–${end} of ${total}`;
          document.getElementById("auditPrevBtn").disabled = page <= 1;
          document.getElementById("auditNextBtn").disabled =
            page >= auditTotalPages;
          paging.style.display = total > AUDIT_PAGE_SIZE ? "flex" : "none";
        } catch (e) {
          loading.style.display = "none";
          empty.style.display = "block";
          empty.textContent =
            "Failed to load: " + (e.message || "Unknown error");
          toast("Failed to load audit data", "error");
        }
      }

      function openAuditDrawer(req) {
        const fmt = (s) => {
          if (!s) return "—";
          const d = new Date(s);
          return (
            d.toLocaleDateString("en-IN", {
              day: "numeric",
              month: "short",
              year: "numeric",
            }) +
            " " +
            d.toLocaleTimeString("en-IN", {
              hour: "2-digit",
              minute: "2-digit",
            })
          );
        };
        const fmtMin = (m) => {
          if (m == null) return "—";
          const h = Math.floor(m / 60),
            mn = m % 60;
          return h ? `${h}h ${mn}m` : `${mn}m`;
        };
        const actionLabel = {
          submitted: "Submitted",
          l1_approved: "L1 Approved",
          l1_rejected: "L1 Rejected",
          l2_approved: "L2 Approved",
          l2_rejected: "L2 Rejected",
        };

        document.getElementById("drawerTitle").textContent =
          `Audit Trail — ${req.employee_name} · ${req.work_date}`;

        const trail = req.audit_trail || [];
        const summary = `
          <div style="background:#f9fafb;border-radius:8px;padding:14px 16px;margin-bottom:20px;">
            <div style="font-size:12px;color:#6b7280;margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px;">Request Summary</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px;">
              <div><span style="color:#9ca3af;">Actual:</span> ${req.actual_worked}</div>
              <div><span style="color:#9ca3af;">Requested:</span> ${req.requested}</div>
              <div><span style="color:#9ca3af;">Reason:</span> ${req.reason}</div>
              <div><span style="color:#9ca3af;">Final:</span> <span class="audit-status-badge ${req.final_status}">${req.final_status}</span></div>
            </div>
            <div style="margin-top:10px;font-size:13px;">
              <span style="color:#9ca3af;">Attendance change:</span>
              <span style="color:#dc2626;">${fmtMin(trail[0]?.minutes_before)}</span>
              <span style="color:#9ca3af;margin:0 6px;">→</span>
              <span style="color:#15803d;">${fmtMin(req.current_total_minutes)}</span>
              <span style="color:#9ca3af;margin-left:6px;">(${req.current_payroll_status})</span>
            </div>
          </div>`;

        const entries = trail.length
          ? trail
              .map(
                (a) => `
            <div class="trail-entry">
              <div class="trail-entry-header">
                <span class="trail-role-badge ${a.action_role}">${a.action_role.toUpperCase()}</span>
                <span class="trail-action-type">${actionLabel[a.action_type] || a.action_type}</span>
                <span class="trail-time">${fmt(a.created_at)}</span>
              </div>
              <div class="trail-actor">by ${a.actioned_by || "System"}</div>
              ${a.note ? `<div class="trail-note">"${a.note}"</div>` : ""}
              ${
                a.minutes_before != null && a.minutes_after != null
                  ? `
                <div class="trail-snapshot">
                  <div class="snap-before">⏮ ${fmtMin(a.minutes_before)} · ${a.payroll_status_before || "—"}</div>
                  <span class="snap-arrow">→</span>
                  <div class="snap-after">✓ ${fmtMin(a.minutes_after)} · ${a.payroll_status_after || "—"}</div>
                </div>`
                  : ""
              }
            </div>`,
              )
              .join("")
          : `<div style="text-align:center;padding:32px;color:#9ca3af;font-size:13px;">No audit entries yet</div>`;

        document.getElementById("drawerContent").innerHTML = summary + entries;
        document.getElementById("auditDrawerOverlay").style.display = "block";
        document.getElementById("auditDrawer").style.display = "block";
      }

      function closeAuditDrawer() {
        document.getElementById("auditDrawerOverlay").style.display = "none";
        document.getElementById("auditDrawer").style.display = "none";
      }
      // ══════════════════════════════════════════════════════════════
      // LEAVE ACTIVITY LOG (HR read-only view)
      // ══════════════════════════════════════════════════════════════

      function initLeaveApprovals() {
        // Default to current month
        const now = new Date();
        const mm = String(now.getMonth() + 1).padStart(2, '0');
        document.getElementById('leaveFilterMonth').value = `${now.getFullYear()}-${mm}`;
        loadLeaveLog();
      }

      function leaveFmtDate(s) {
        if (!s) return '—';
        const [y, mo, d] = String(s).split('T')[0].split('-');
        const M = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        return `${parseInt(d)} ${M[parseInt(mo)-1]} ${y}`;
      }

      function leaveStatusBadge(s) {
        const map = {
          approved:  'background:#d1fae5;color:#065f46;',
          rejected:  'background:#fee2e2;color:#991b1b;',
          pending:   'background:#fef3c7;color:#92400e;',
          cancelled: 'background:#f3f4f6;color:#374151;',
          na:        'background:#f3f4f6;color:#9ca3af;',
        };
        const style = map[s] || '';
        return `<span style="font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;${style}">${s || '—'}</span>`;
      }

      function leaveTypeBadge(t) {
        return t === 'paid'
          ? `<span style="font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;background:#dbeafe;color:#1e40af;">Paid</span>`
          : `<span style="font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;background:#f3f4f6;color:#374151;">Unpaid</span>`;
      }

      function leaveEmpColor(name) {
        const COLS = ['#4f46e5','#0891b2','#059669','#d97706','#dc2626','#7c3aed'];
        return COLS[(name?.charCodeAt(0) || 65) % COLS.length];
      }

      async function loadLeaveLog() {
        const loading = document.getElementById('leaveLoading');
        const table   = document.getElementById('leaveTable');
        const empty   = document.getElementById('leaveEmpty');
        loading.style.display = 'flex';
        table.style.display   = 'none';
        empty.style.display   = 'none';

        const params = new URLSearchParams();
        const monthVal  = document.getElementById('leaveFilterMonth').value;
        const statusVal = document.getElementById('leaveFilterStatus').value;
        if (monthVal) {
          const [y, m] = monthVal.split('-');
          params.set('year', y);
          params.set('month', parseInt(m, 10));
        }
        if (statusVal && statusVal !== 'all') params.set('status', statusVal);

        try {
          const data = await apiFetch(`/leave/hr/requests?${params}`);
          const rows = data.requests || [];

          // KPI counts
          const pending   = rows.filter(r => r.final_status === 'pending').length;
          const approved  = rows.filter(r => r.final_status === 'approved').length;
          const rejected  = rows.filter(r => r.final_status === 'rejected').length;
          document.getElementById('leaveKpiPending').textContent  = pending;
          document.getElementById('leaveKpiApproved').textContent = approved;
          document.getElementById('leaveKpiRejected').textContent = rejected;
          document.getElementById('leaveKpiTotal').textContent    = rows.length;

          loading.style.display = 'none';

          if (!rows.length) {
            empty.style.display = 'block';
            empty.textContent = 'No leave requests found for the selected filters.';
            return;
          }

          table.style.display = 'table';
          document.getElementById('leaveTbody').innerHTML = rows.map(r => {
            const name   = r.employee_name || '?';
            const letter = name[0].toUpperCase();
            const color  = leaveEmpColor(name);
            return `<tr>
              <td>
                <div class="emp-cell">
                  <div class="emp-avatar" style="background:${color};flex-shrink:0;">${letter}</div>
                  <div>
                    <div class="emp-name">${name}</div>
                    <div class="emp-role" style="font-size:11px;color:#9ca3af;">${r.employee_id || ''}</div>
                  </div>
                </div>
              </td>
              <td style="white-space:nowrap;font-size:13px;">
                ${leaveFmtDate(r.date_from)}<br>
                <span style="color:#9ca3af;font-size:11px;">to ${leaveFmtDate(r.date_to)}</span>
              </td>
              <td style="font-weight:600;text-align:center;">${r.num_days}</td>
              <td>${leaveTypeBadge(r.leave_type)}</td>
              <td style="max-width:160px;font-size:12px;color:#374151;">${r.reason || '—'}</td>
              <td style="font-size:12px;color:#374151;">${r.l1_manager_name || '—'}</td>
              <td>${leaveStatusBadge(r.l1_status)}</td>
              <td style="font-size:12px;color:#374151;">${r.l2_manager_name || '—'}</td>
              <td>${leaveStatusBadge(r.l2_status)}</td>
              <td>${leaveStatusBadge(r.final_status)}</td>
            </tr>`;
          }).join('');

        } catch(e) {
          loading.style.display = 'none';
          empty.style.display = 'block';
          empty.textContent = 'Failed to load: ' + (e.message || 'Unknown error');
          toast('Failed to load leave activity', 'error');
        }
      }