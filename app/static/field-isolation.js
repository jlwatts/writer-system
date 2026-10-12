(function (root) {
    function disabledDuringFocus(isActive) {
        if (isActive) return false;
        return true;
    }

    function disabledAfterBlur(wasDisabled) {
        if (wasDisabled) return true;
        return false;
    }

    function shouldKeepDisabled(wasDisabled, findBarClosed, isFindField) {
        if (isFindField && findBarClosed) return true;
        if (wasDisabled) return true;
        return false;
    }

    root.WriterFields = {
        disabledDuringFocus: disabledDuringFocus,
        disabledAfterBlur: disabledAfterBlur,
        shouldKeepDisabled: shouldKeepDisabled,
    };
}(typeof window !== 'undefined' ? window : this));
