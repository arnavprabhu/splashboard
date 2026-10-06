import { beforeEach, describe, expect, it } from 'vitest';
import { adminPath, adminReturnPath } from '../src/routes/chat/returnPath';

const O = location.origin;

describe('chat logo return path', () => {
  beforeEach(() => sessionStorage.clear());

  it('accepts same-origin admin pages, never the chat or full-page flows', () => {
    expect(adminPath(`${O}/admin/models?tab=x`, '/admin', O)).toBe('/models?tab=x');
    expect(adminPath(`${O}/admin/chat/c1`, '/admin', O)).toBeNull();
    expect(adminPath(`${O}/admin/chat`, '/admin', O)).toBeNull();
    expect(adminPath(`${O}/admin/login?next=%2F`, '/admin', O)).toBeNull();
    expect(adminPath(`${O}/admin/welcome`, '/admin', O)).toBeNull();
    expect(adminPath('https://example.com/admin/models', '/admin', O)).toBeNull();
    expect(adminPath(`${O}/v1/models`, '/admin', O)).toBeNull();
    expect(adminPath('', '/admin', O)).toBeNull();
  });

  it('returns to the newest admin page in this tab’s history, skipping chat entries', () => {
    const history = [`${O}/admin/chat/c1`, `${O}/admin/logs`, `${O}/admin/status`];
    expect(adminReturnPath('/admin', { history, referrer: '', stored: null })).toBe('/logs');
    expect(sessionStorage.getItem('chat.return')).toBe('/logs');
  });

  it('falls back to the referrer, then what this tab remembered, then Status', () => {
    expect(adminReturnPath('/admin', { history: [], referrer: `${O}/admin/integrations`, stored: '/logs' })).toBe('/integrations');
    expect(adminReturnPath('/admin', { history: [], referrer: '', stored: '/logs' })).toBe('/logs');
    expect(adminReturnPath('/admin', { history: [], referrer: 'https://elsewhere.test/', stored: null })).toBe('/status');
    expect(adminReturnPath('/admin', { history: [], referrer: '', stored: '/chat/c2' })).toBe('/status');
  });
});
