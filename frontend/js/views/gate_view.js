export const gateViewTemplate = `
<section class="panel">
  <h2>闸口放行</h2>
  <p class="hint">须票态已裁定且超重账无开放挂起；超重未清禁放，服务端整笔回滚、正式放行册零增。两人同票放行抢闸口锁，败方只记争用痕。</p>
  <div class="form-row">
    <div><label>过磅票</label>
      <select v-model.number="gateForm.ticket_id">
        <option v-for="t in releasableTickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>值班员</label><input v-model="gateForm.officer"/></div>
  </div>
  <div class="actions">
    <button @click="releaseGate">确认放行</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <div v-if="gateBlockedTickets.length" class="detail-box">
    <p class="hint" style="margin:0 0 8px">禁放（超重未清/旁路未闭环）：闭环超重后方可进放行候选。</p>
    <table class="data-table">
      <thead><tr><th>票号</th><th>车道</th><th>票面</th><th>开放超重</th><th>口径</th></tr></thead>
      <tbody>
        <tr v-for="t in gateBlockedTickets" :key="t.id">
          <td>{{ t.ticket_code }}</td>
          <td>{{ t.lane_code }}</td>
          <td>{{ t.stored_status }}</td>
          <td>{{ t.open_holds }} 条开放</td>
          <td><span class="badge fail">{{ t.status }}</span></td>
        </tr>
      </tbody>
    </table>
  </div>
  <table class="data-table">
    <thead><tr><th>放行号</th><th>票号</th><th>车道</th><th>值班员</th><th>时间</th></tr></thead>
    <tbody>
      <tr v-for="g in gateReleases" :key="g.id">
        <td>{{ g.release_ref }}</td>
        <td>{{ g.ticket_code }}</td>
        <td>{{ g.lane_code }}</td>
        <td>{{ g.officer }}</td>
        <td>{{ g.released_at }}</td>
      </tr>
    </tbody>
  </table>
</section>
`;