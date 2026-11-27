
(() => {
    "use strict";

    const AUTO_CLOSE_DELAY = 5000;
    const REMOVE_ANIMATION_DELAY = 220;

    function initToast(toast) {
        const closeButton = toast.querySelector("[data-toast-close]");

        let remainingTime = AUTO_CLOSE_DELAY;
        let timerId = null;
        let startedAt = null;
        let closed = false;

        function stopTimer() {
            if (timerId === null) {
                return;
            }

            clearTimeout(timerId);
            timerId = null;

            remainingTime -= Date.now() - startedAt;
        }

        function startTimer() {
            if (closed || timerId !== null) {
                return;
            }

            startedAt = Date.now();

            timerId = setTimeout(() => {
                closeToast();
            }, Math.max(remainingTime, 0));
        }

        function closeToast() {
            if (closed) {
                return;
            }

            closed = true;
            stopTimer();

            toast.classList.remove("toast-notification--visible");
            toast.classList.add("toast-notification--leaving");

            setTimeout(() => {
                toast.remove();
            }, REMOVE_ANIMATION_DELAY);
        }

        // Наведение мыши приостанавливает отсчёт времени.
        toast.addEventListener("mouseenter", stopTimer);
        toast.addEventListener("mouseleave", startTimer);

        // Закрытие по крестику.
        closeButton?.addEventListener("click", closeToast);

        // Появление запускаем после применения начальных CSS-состояний.
        requestAnimationFrame(() => {
            if (closed) {
                return;
            }

            toast.classList.add("toast-notification--visible");
            startTimer();
        });
    }

    function initFlashNotifications() {
        const toasts = document.querySelectorAll("[data-toast]");

        toasts.forEach(initToast);
    }

    if (document.readyState === "loading") {
        document.addEventListener(
            "DOMContentLoaded",
            initFlashNotifications,
            { once: true }
        );
    } else {
        initFlashNotifications();
    }
})();
