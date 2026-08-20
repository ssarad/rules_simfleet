#import <UIKit/UIKit.h>

@interface SimFleetAppDelegate : UIResponder <UIApplicationDelegate>
@property(nonatomic, strong) UIWindow *window;
@end

@implementation SimFleetAppDelegate

- (BOOL)application:(UIApplication *)application
    didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
  (void)application;
  (void)launchOptions;

  UIViewController *viewController = [[UIViewController alloc] init];
  viewController.view.backgroundColor = UIColor.systemBackgroundColor;

  UILabel *label = [[UILabel alloc] init];
  label.translatesAutoresizingMaskIntoConstraints = NO;
  label.text = @"SimFleet ready";
  label.accessibilityIdentifier = @"SIMFLEET_READY";
  [viewController.view addSubview:label];
  [NSLayoutConstraint activateConstraints:@[
    [label.centerXAnchor constraintEqualToAnchor:viewController.view.centerXAnchor],
    [label.centerYAnchor constraintEqualToAnchor:viewController.view.centerYAnchor],
  ]];

  self.window = [[UIWindow alloc] initWithFrame:UIScreen.mainScreen.bounds];
  self.window.rootViewController = viewController;
  [self.window makeKeyAndVisible];
  return YES;
}

@end

int main(int argc, char *argv[]) {
  @autoreleasepool {
    return UIApplicationMain(argc, argv, nil, NSStringFromClass(SimFleetAppDelegate.class));
  }
}
