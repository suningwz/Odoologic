# Copyright 2020 Manish Kumar Bohra <manishbohra1994@gmail.com> or <manishkumarbohra@outlook.com>
# License LGPL-3 - See http://www.gnu.org/licenses/Lgpl-3.0.html
import odoo
from odoo import api, fields, models, _
import threading
import time
import logging

_logger = logging.getLogger(__name__)

class LockingCounter():
    def __init__(self):
        self.lock = threading.Lock()
        self.count = 0

    def increment(self):
        with self.lock:
            self.count += 1

    def get_count(self):
        return self.count


class GLSDataAggregator():
    def __init__(self):
        self.lock = threading.Lock()
        self.GLSData = []

    def add_data(self,data):
        with self.lock:
            self.GLSData.append(data)

    def get_data(self):
        return self.GLSData

class CreateLabelWizard(models.TransientModel):
    _name = 'create_label.wizard'

    sale_orders_not_confirmed = fields.Char(readonly=True)
    sale_orders_without_label = fields.Char(readonly=True)
    hide_confirm_button = fields.Boolean()
    nr_threads = fields.Selection([('0', 'No threads'), ('3', '3 Threads'), ('5', '5 Threads'), ('7', '7 Threads'), ('10', '10 Threads'),('30', '30 Threads')],
                                  string="Number of threads", default='0')

    def progressbar(self, sale_orders, counter, label):
        j = 1
        for sale_order in self.web_progress_iter(range(0,len(sale_orders)), msg=label):
            j = j + 1
            while 1 == 1:
                time.sleep(0.05)
                if counter.get_count() > j or counter.get_count() == len(sale_orders):
                    break;

    def get_stats(self, orders):
        with api.Environment.manage():
            # As this function is in a new thread, I need to open a new cursor, because the old one may be closed
            wizard = self
            new_cr = self.pool.cursor()
            self = self.with_env(self.env(cr=new_cr))
            pickings_without_track = self.env['stock.picking'].search(
                [('sale_id', 'in', orders), ('label_created', '!=', True)]).mapped('sale_id.name')
            #print(pickings_without_track1[0].label_created)
            orders_not_confirmed = self.env['sale.order'].browse(orders).filtered(
                lambda l: l.state != 'sale').mapped('name')

            new_cr.close()
            return [pickings_without_track, orders_not_confirmed]

    def thread_function(self, batch, counter, GLSData):
        with api.Environment.manage():
            # As this function is in a new thread, I need to open a new cursor, because the old one may be closed
            new_cr = self.pool.cursor()
            self = self.with_env(self.env(cr=new_cr))
            orders_without_labels = []
            sale_orders = batch
            if len(sale_orders) > 0:
                for sale_order in sale_orders:
                    order = self.env['sale.order'].search([('id', '=', sale_order.id)], limit=1)
                    print(order.name)
                    pickings = order.mapped('picking_ids')
                    picking_id = pickings.filtered(lambda l: l.picking_type_id.code == 'outgoing' and l.state != 'cancel')
                    if picking_id:
                        if len(picking_id) == 1:
                            if order.partner_id.property_delivery_carrier_id:
                                if not picking_id.carrier_id:
                                    picking_id.carrier_id = order.partner_id.property_delivery_carrier_id
                            if 'GLS' in picking_id.carrier_id.name or 'GLS NL' in picking_id.carrier_id.name:
                                try:
                                    GLSData.add_data(picking_id.carrier_id.gls_send_shipping(picking_id))
                                    #picking_id.label_created = True
                                except Exception as e:
                                    _logger.info("Error: %s", e)
                                    picking_id.message_post(body=e)
                                    #picking_id.label_created = False
                        if picking_id.carrier_tracking_ref:
                            order.t_n_t_code = picking_id.carrier_tracking_ref
                            order.tracking_no = picking_id.tracking_no

                    print(picking_id)
                    counter.increment()
            new_cr.commit()
            new_cr.close()
            return {}

    def confirm_saleorders(self,batch,counter):
        with api.Environment.manage():
            # As this function is in a new thread, I need to open a new cursor, because the old one may be closed
            new_cr = self.pool.cursor()
            self = self.with_env(self.env(cr=new_cr))
            sale_orders = batch
            if len(sale_orders) > 0:
                for sale_order in sale_orders:
                    order = self.env['sale.order'].search([('id', '=', sale_order.id)], limit=1)
                    try:
                        order.with_context(batch_process=True).action_confirm()
                    except Exception as e:
                        _logger.info("Error: %s", e)
                        # put message in sale order, "There is no delivery method set on the partner!"
                        print("post message in order")
                        order.message_post(body=e)
                    counter.increment()
                    new_cr.commit()
            new_cr.close()
            return {}

    def split_list(self, alist, wanted_parts=1):
        length = len(alist)
        return [alist[i * length // wanted_parts: (i + 1) * length // wanted_parts] for i in range(wanted_parts)]

    def bulk_sales_order_approve(self):

        orders_without_labels = []
        active_ids = self.env.context.get('active_ids', []) or []
        sale_orders = self.env['sale.order'].browse(active_ids)
        _logger.info("THREADS:" + str(self.nr_threads))

        j = 1

        if int(self.nr_threads) > 1:
            #self.env.cr.commit()
            #
            batches = self.split_list(sale_orders, wanted_parts=int(self.nr_threads))

            threads = list()
            i = 0
            counter = LockingCounter()
            GLSData = GLSDataAggregator()

            x = threading.Thread(target=self.progressbar, args=(sale_orders, counter, "Step 1/2 -> Confirming sale orders ..."))
            threads.append(x)
            x.start()

            x = threading.Thread(target=self.confirm_saleorders, args=(sale_orders, counter))
            threads.append(x)
            x.start()

            for index, thread in enumerate(threads):
                _logger.info("Main    : before joining thread %d.", i)
                thread.join()
                _logger.info("Main    : thread %d done", i)


            counter.count = 0
            #self.env.cr.commit()
            threads = list()

            time.sleep(5)  # give some time for the progressbar
            x = threading.Thread(target=self.progressbar, args=(sale_orders, counter, "Step 2/2 -> Retrieving labels from GLS ..."))
            threads.append(x)
            x.start()

            for batch in batches:
                i = i + 1
                _logger.info("Main    : create and start thread %d.", i)
                x = threading.Thread(target=self.thread_function, args=(batch, counter, GLSData))
                threads.append(x)
                x.start()

            for index, thread in enumerate(threads):
                _logger.info("Main    : before joining thread %d.", i)
                thread.join()
                _logger.info("Main    : thread %d done", i)

            #_logger.info("Data: " + str(GLSData.get_data()))

            self.env.cr.commit()
            # time.sleep(2)
            for shipment_result in GLSData.get_data():
                picking = self.env['stock.picking'].browse(shipment_result[0]['picking_id'])

                picking.tracking_link = shipment_result[0]['response_body']['units'][0]['unitTrackingLink']
                tracking_number = shipment_result[0]['response_body']['units'][0]['unitNo']
                picking.carrier_tracking_ref = shipment_result[0]['response_body']['units'][0]['unitNo']
                picking.tracking_no = shipment_result[0]['response_body']['units'][0]['uniqueNo']
                picking.label_created = True
                #_logger.error("Data: " + str(picking.sale_id))
                picking.sale_id.t_n_t_code = shipment_result[0]['response_body']['units'][0]['unitNo']
                picking.sale_id.tracking_no = shipment_result[0]['response_body']['units'][0]['uniqueNo']
                # store zpl file
                log_message = (_("GLS Shipping Created <br/> <b>Tracking Number : </b>%s") % (tracking_number))
                picking.message_post(body=log_message, attachments=[('%s.zpl' % tracking_number,
                                                                     shipment_result[0]['label'])])

                self.env.cr.commit()
        else:
            for sale_order in self.web_progress_iter(sale_orders, msg="Confirming sale orders and retrieving labels from GLS ..."):
                try:
                    sale_order.with_context(batch_process=False).action_confirm()
                except Exception as e:
                    _logger.info("Error: %s", e)
                    sale_order.message_post(body=e)
                self.env.cr.commit()



        #self.env.cr.commit()

        pickings_without_track, orders_not_confirmed = self.get_stats(active_ids)

        if pickings_without_track:
            if len(pickings_without_track) >= 1:
                print("pickings without track:", pickings_without_track)
                self.sale_orders_without_label = "Labels are not made for: " + ", ".join(pickings_without_track)

        if orders_not_confirmed:
            if len(orders_not_confirmed) >= 1:
                print("orders not confirmed:", orders_not_confirmed)
                self.sale_orders_not_confirmed = "Orders are not confirmed for: " + ", ".join(orders_not_confirmed)

        window_name = self.env['ir.actions.act_window'].search([('res_model', '=', self._name)], limit=1).name
        self.hide_confirm_button = True
        return {
            'name': window_name,
            'view_mode': 'form',
            'view_id': False,
            'res_model': self._name,
            'domain': [],
            'context': dict(self._context, active_ids=self.ids),
            'type': 'ir.actions.act_window',
            'target': 'new',
            'res_id': self.id,
        }

    def action_close(self):
        """ close wizard"""
        return {'type': 'ir.actions.act_window_close'}
