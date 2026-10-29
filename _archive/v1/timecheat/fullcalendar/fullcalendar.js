// FullCalendar wrapped as a NiceGUI (Vue) component.
//
// Python owns all state; this component only renders `events` and `dayInfo`
// and reports user gestures back as `tc_*` events. JS callbacks that
// FullCalendar needs (cell colouring, custom event content) live here because
// options passed from Python can only be plain JSON.

function ensureFullCalendar(url) {
  if (!window.__timecheatFullCalendar) {
    window.__timecheatFullCalendar = new Promise((resolve, reject) => {
      if (window.FullCalendar) return resolve();
      const script = document.createElement("script");
      script.src = url;
      script.onload = () => resolve();
      script.onerror = () => reject(new Error(`Cannot load ${url}`));
      document.head.appendChild(script);
    });
  }
  return window.__timecheatFullCalendar;
}

function ymd(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function localIso(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${ymd(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}:00`;
}

export default {
  template: "<div></div>",
  props: {
    options: Object,
    events: Array,
    dayInfo: Object,
    resourcePath: String,
  },
  async mounted() {
    await this.$nextTick();
    await ensureFullCalendar(window.path_prefix + `${this.resourcePath}/index.global.min.js`);
    const options = Object.assign({}, this.options, this.cellCallbacks(), {
      events: (_info, success) => success(this.events || []),
      eventContent: (arg) => ({ html: arg.event.extendedProps.html || arg.event.title }),
      eventDidMount: (info) => {
        if (info.event.extendedProps.tooltip) info.el.title = info.event.extendedProps.tooltip;
      },
      select: (info) =>
        this.$emit("tc_select", { start: localIso(info.start), end: localIso(info.end) }),
      dateClick: (info) => this.$emit("tc_date_click", { date: ymd(info.date) }),
      eventClick: (info) =>
        this.$emit("tc_event_click", { id: info.event.id, date: ymd(info.event.start) }),
      eventDrop: (info) => this.emitChange(info),
      eventResize: (info) => this.emitChange(info),
    });
    this.calendar = new FullCalendar.Calendar(this.$el, options);
    this.calendar.render();
    if (this.options.scrollTime) this.calendar.scrollToTime(this.options.scrollTime);
    // The NiceGUI layout settles after mount; keep FullCalendar's measurements in sync
    // and scroll to the start of the working day once the real height is known.
    this.resizeObserver = new ResizeObserver(() => {
      if (!this.calendar) return;
      this.calendar.updateSize();
      if (!this.initiallyScrolled && this.$el.clientHeight > 100 && this.options.scrollTime) {
        this.initiallyScrolled = true;
        this.calendar.scrollToTime(this.options.scrollTime);
      }
    });
    this.resizeObserver.observe(this.$el);
  },
  beforeUnmount() {
    if (this.resizeObserver) this.resizeObserver.disconnect();
    if (this.calendar) this.calendar.destroy();
  },
  watch: {
    events() {
      if (this.calendar) this.calendar.refetchEvents();
    },
    dayInfo() {
      if (!this.calendar) return;
      // Fresh closures make FullCalendar re-render the day cells.
      const callbacks = this.cellCallbacks();
      for (const [name, fn] of Object.entries(callbacks)) this.calendar.setOption(name, fn);
    },
  },
  methods: {
    cellCallbacks() {
      const info = this.dayInfo || {};
      return {
        dayCellClassNames: (arg) => {
          const day = info[ymd(arg.date)];
          return day && day.cls ? day.cls.split(" ") : [];
        },
        dayCellContent: (arg) => {
          if (arg.view.type !== "dayGridMonth") return true;
          const day = info[ymd(arg.date)];
          const label = day && day.label ? `<span class="tc-day-sum">${day.label}</span>` : "";
          const note = day && day.note ? `<span class="tc-day-note" title="${day.note}">${day.note}</span>` : "";
          return { html: `${note}${label}<span class="tc-day-num">${arg.dayNumberText}</span>` };
        },
      };
    },
    emitChange(info) {
      this.$emit("tc_event_change", {
        id: info.event.id,
        start: localIso(info.event.start),
        end: localIso(info.event.end),
      });
    },
    gotoDate(date) {
      if (!this.calendar) return;
      this.calendar.gotoDate(date);
      if (this.options.scrollTime) this.calendar.scrollToTime(this.options.scrollTime);
    },
    unselect() {
      if (this.calendar) this.calendar.unselect();
    },
    scrollToTime(time) {
      if (this.calendar) this.calendar.scrollToTime(time);
    },
    updateSize() {
      if (this.calendar) this.calendar.updateSize();
    },
  },
};
