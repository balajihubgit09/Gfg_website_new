const body = document.body;
const header = document.querySelector('.site-header');
const overflowMenu = document.querySelector('.overflow-menu');
const overflowTrigger = document.querySelector('.overflow-trigger');
const overflowPanel = document.querySelector('.overflow-panel');
const adminTrigger = document.querySelector('[data-admin-trigger]');
const themeOptions = document.querySelectorAll('[data-theme-choice]');
const navLinks = document.querySelectorAll('.nav a');
const glassCards = document.querySelectorAll('.glass-card');
const animatedCounters = document.querySelectorAll('.metric-value');
const countdownPills = document.querySelectorAll('[data-countdown]');
const passwordToggles = document.querySelectorAll('[data-password-toggle]');
const forms = document.querySelectorAll('.modern-form');
const flashMessages = document.querySelectorAll('.flash');
const THEME_KEY = 'gfg-theme';

if (header) {
    const syncHeaderState = () => {
        header.classList.toggle('is-scrolled', window.scrollY > 12);
    };

    syncHeaderState();
    window.addEventListener('scroll', syncHeaderState, { passive: true });
}

const setOpenState = (isOpen) => {
    if (!overflowTrigger || !overflowPanel || !overflowMenu) {
        return;
    }

    overflowTrigger.setAttribute('aria-expanded', String(isOpen));
    overflowPanel.hidden = !isOpen;
    overflowMenu.classList.toggle('is-open', isOpen);
};

const applyTheme = (theme) => {
    body.dataset.theme = theme;

    themeOptions.forEach((option) => {
        option.classList.toggle('is-active', option.dataset.themeChoice === theme);
    });

    window.localStorage.setItem(THEME_KEY, theme);
};

const currentPath = window.location.pathname;
navLinks.forEach((link) => {
    const url = new URL(link.href, window.location.origin);
    if (url.pathname === currentPath) {
        link.classList.add('is-active');
        link.setAttribute('aria-current', 'page');
    }
});

applyTheme(window.localStorage.getItem(THEME_KEY) === 'white' ? 'white' : 'dark');
setOpenState(false);

if (adminTrigger) {
    adminTrigger.addEventListener('dblclick', () => {
        const adminUrl = adminTrigger.dataset.adminUrl;
        if (adminUrl) {
            window.location.assign(adminUrl);
        }
    });
}

if (overflowTrigger) {
    overflowTrigger.addEventListener('click', () => {
        const isOpen = overflowTrigger.getAttribute('aria-expanded') === 'true';
        setOpenState(!isOpen);
    });
}

document.addEventListener('click', (event) => {
    if (!overflowMenu || overflowPanel.hidden) {
        return;
    }

    if (!overflowMenu.contains(event.target)) {
        setOpenState(false);
    }
});

themeOptions.forEach((option) => {
    option.addEventListener('click', () => {
        applyTheme(option.dataset.themeChoice);
    });
});

passwordToggles.forEach((toggle) => {
    toggle.addEventListener('click', () => {
        const target = document.getElementById(toggle.dataset.passwordToggle);
        if (!target) {
            return;
        }

        const nextType = target.type === 'password' ? 'text' : 'password';
        target.type = nextType;
        toggle.textContent = nextType === 'password' ? 'Show' : 'Hide';
        toggle.setAttribute('aria-label', nextType === 'password' ? 'Show password' : 'Hide password');
    });
});

forms.forEach((form) => {
    form.addEventListener('submit', (event) => {
        if (!form.checkValidity()) {
            return;
        }

        const submitButton = form.querySelector('button[type="submit"]');
        if (!submitButton) {
            return;
        }

        submitButton.dataset.originalText = submitButton.textContent;
        submitButton.textContent = submitButton.dataset.loadingText || 'Working...';
        submitButton.disabled = true;
        form.classList.add('is-submitting');
    });
});

glassCards.forEach((card) => {
    card.addEventListener('mousemove', (event) => {
        const rect = card.getBoundingClientRect();
        card.style.setProperty('--mouse-x', `${event.clientX - rect.left}px`);
        card.style.setProperty('--mouse-y', `${event.clientY - rect.top}px`);
    });

    card.addEventListener('mouseleave', () => {
        card.style.removeProperty('--mouse-x');
        card.style.removeProperty('--mouse-y');
    });
});

if (typeof IntersectionObserver === 'function') {
    const counterObserver = new IntersectionObserver(
        (entries, observer) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) {
                    return;
                }

                const element = entry.target;
                const target = Number(element.dataset.count || '0');
                const duration = 950;
                const start = performance.now();

                const tick = (now) => {
                    const progress = Math.min((now - start) / duration, 1);
                    const eased = 1 - Math.pow(1 - progress, 3);
                    element.textContent = Math.floor(eased * target).toString();

                    if (progress < 1) {
                        window.requestAnimationFrame(tick);
                    } else {
                        element.textContent = target.toString();
                    }
                };

                window.requestAnimationFrame(tick);
                observer.unobserve(element);
            });
        },
        { threshold: 0.35 }
    );

    animatedCounters.forEach((counter) => {
        counterObserver.observe(counter);
    });
}

countdownPills.forEach((pill) => {
    const rawDate = pill.dataset.countdown;
    const target = new Date(`${rawDate}T23:59:59`);

    if (Number.isNaN(target.getTime())) {
        return;
    }

    const now = new Date();
    const diffDays = Math.ceil((target - now) / (1000 * 60 * 60 * 24));

    if (diffDays > 1) {
        pill.textContent = `Website Challenge: ${diffDays} days left`;
    } else if (diffDays === 1) {
        pill.textContent = 'Website Challenge: 1 day left';
    } else if (diffDays === 0) {
        pill.textContent = 'Website Challenge: today';
    } else {
        pill.textContent = `Next event: ${rawDate}`;
    }
});

flashMessages.forEach((message) => {
    window.setTimeout(() => {
        message.classList.add('is-hiding');
        window.setTimeout(() => {
            message.remove();
        }, 260);
    }, 4200);
});
