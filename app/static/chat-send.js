(function (root) {
    function bubbleSurvivesHistory(inflightId, bubbleId) {
        if (!inflightId) return false;
        if (!bubbleId) return false;
        return inflightId === bubbleId;
    }

    function buttonEnabledAfter(phase) {
        if (phase === 'success') return true;
        if (phase === 'error') return true;
        if (phase === 'timeout') return true;
        return false;
    }

    function chatTimeoutMs() {
        return 180000;
    }

    root.WriterChat = {
        bubbleSurvivesHistory: bubbleSurvivesHistory,
        buttonEnabledAfter: buttonEnabledAfter,
        chatTimeoutMs: chatTimeoutMs,
    };
}(typeof window !== 'undefined' ? window : this));
