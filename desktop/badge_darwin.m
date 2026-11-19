#import <AppKit/AppKit.h>

void huginn_set_dock_badge(int count) {
    dispatch_async(dispatch_get_main_queue(), ^{
        NSApp.dockTile.badgeLabel = count > 0 ? [NSString stringWithFormat:@"%d", count] : nil;
    });
}
