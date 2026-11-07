package com.aether.earth;

import android.content.Context;
import android.util.AttributeSet;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;
import android.webkit.WebView;
import android.widget.FrameLayout;
import android.widget.ProgressBar;

/**
 * Minimal pull-to-refresh wrapper (no external dependencies).
 * Shows a spinner when the user pulls down while the WebView is scrolled to the top.
 */
public class PullRefreshLayout extends FrameLayout {

    public interface OnRefreshListener {
        void onRefresh();
    }

    private ProgressBar spinner;
    private OnRefreshListener listener;
    private float downY = -1;
    private boolean dragging = false;
    private boolean refreshing = false;
    private final int touchSlop;
    private int spinnerHeight;

    public PullRefreshLayout(Context context) {
        this(context, null);
    }

    public PullRefreshLayout(Context context, AttributeSet attrs) {
        super(context, attrs);
        touchSlop = ViewConfiguration.get(context).getScaledTouchSlop();
        float density = context.getResources().getDisplayMetrics().density;
        spinnerHeight = (int) (56 * density);
        spinner = new ProgressBar(context);
        LayoutParams lp = new LayoutParams(
                LayoutParams.WRAP_CONTENT, LayoutParams.WRAP_CONTENT);
        lp.gravity = android.view.Gravity.TOP | android.view.Gravity.CENTER_HORIZONTAL;
        lp.topMargin = (int) (8 * density);
        spinner.setLayoutParams(lp);
        spinner.setTranslationY(-spinnerHeight * 2);
        addView(spinner);
    }

    public void setOnRefreshListener(OnRefreshListener l) {
        this.listener = l;
    }

    public void setRefreshing(boolean refreshing) {
        this.refreshing = refreshing;
        if (!refreshing && !dragging) {
            resetPositions();
        } else if (refreshing) {
            spinner.setTranslationY(0);
            View content = getContentView();
            if (content != null) content.setTranslationY(spinnerHeight);
        }
    }

    private View getContentView() {
        for (int i = 0; i < getChildCount(); i++) {
            View c = getChildAt(i);
            if (c instanceof WebView) return c;
        }
        return null;
    }

    private boolean canChildScrollUp() {
        View content = getContentView();
        return content != null && content.canScrollVertically(-1);
    }

    private void resetPositions() {
        spinner.animate().translationY(-spinnerHeight * 2).setDuration(200).start();
        View content = getContentView();
        if (content != null) content.animate().translationY(0).setDuration(200).start();
    }

    @Override
    public boolean onInterceptTouchEvent(MotionEvent ev) {
        if (refreshing) return false;
        switch (ev.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                downY = ev.getY();
                dragging = false;
                break;
            case MotionEvent.ACTION_MOVE:
                if (downY < 0) break;
                float dy = ev.getY() - downY;
                if (dy > touchSlop && !canChildScrollUp()) {
                    dragging = true;
                    return true;
                }
                break;
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL:
                downY = -1;
                dragging = false;
                break;
        }
        return super.onInterceptTouchEvent(ev);
    }

    @Override
    public boolean onTouchEvent(MotionEvent ev) {
        if (!dragging) return super.onTouchEvent(ev);
        switch (ev.getActionMasked()) {
            case MotionEvent.ACTION_MOVE: {
                float dy = Math.max(0, ev.getY() - downY);
                float offset = Math.min(dy * 0.5f, spinnerHeight * 2.2f);
                spinner.setTranslationY(offset - spinnerHeight);
                View content = getContentView();
                if (content != null) content.setTranslationY(offset);
                return true;
            }
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL: {
                float dy = Math.max(0, ev.getY() - downY);
                dragging = false;
                downY = -1;
                if (dy * 0.5f > spinnerHeight && listener != null) {
                    setRefreshing(true);
                    listener.onRefresh();
                } else {
                    resetPositions();
                }
                return true;
            }
        }
        return super.onTouchEvent(ev);
    }
}
